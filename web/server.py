from flask import Flask, render_template, jsonify, send_from_directory
from flask_cors import CORS
import json
from datetime import datetime, timedelta
import logging
from tinydb import TinyDB, Query
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots
import pandas as pd

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)

# Load config
with open('config.json', 'r') as f:
    CONFIG = json.load(f)


@app.route('/')
def index():
    """Render main page"""
    return render_template('index.html')

@app.route('/api/results')
def get_results():
    """Return optimizer results as JSON"""
    with TinyDB('db.json') as db:
        schedule_docs = db.search(Query().id == 'schedule')
    return schedule_docs

@app.route('/api/battery_thresholds')
def get_battery_thresholds():
    """Return the battery LP plan summary (per-device economics for the current horizon)."""
    empty = {
        'method': None,
        'round_trip_efficiency': None,
        'cycle_cost_per_kwh': None,
        'export_pricing': None,
        'devices': {},
        'updated_at': '',
        'last_soc_recalc': '',
    }
    with TinyDB('db.json') as db:
        schedule_docs = db.search(Query().id == 'schedule')

    if schedule_docs and schedule_docs[0].get('battery_plan_summary'):
        summary = schedule_docs[0]['battery_plan_summary']
        return jsonify({
            'method': summary.get('method', 'lp'),
            'round_trip_efficiency': summary.get('round_trip_efficiency'),
            'cycle_cost_per_kwh': summary.get('cycle_cost_per_kwh'),
            'export_pricing': summary.get('export_pricing'),
            'devices': summary.get('devices', {}),
            'updated_at': schedule_docs[0].get('updated_at', ''),
            'last_soc_recalc': schedule_docs[0].get('last_soc_recalc', ''),
        })
    return jsonify(empty)


_EV_DEADLINE_EMPTY_PRIMARY = {
    'device': None,
    'ready': None,
    'ready_condition_configured': False,
    'charging': False,
    'charging_power_w': None,
    'charging_power_kw': None,
    'charging_source': None,
    'charging_condition_configured': False,
    'charging_load_entity': None,
    'scheduled': False,
    'status': 'unavailable',
    'headline': 'No EV deadline plan yet',
    'plan_headline': 'No EV deadline plan yet',
    'message': 'No EV deadline-charging plan has been computed yet.',
    'current_soc': None,
    'target_soc': None,
    'deadline': None,
    'charge_power_kw': None,
    'energy_needed_kwh': 0.0,
    'energy_planned_kwh': 0.0,
    'energy_scheduled_kwh': 0.0,
    'avg_price': None,
    'slot_count': 0,
    'scheduled_slot_count': 0,
    'slots': [],
    'scheduled_slots': [],
    'first_slot_start': None,
    'last_slot_stop': None,
    'updated_at': '',
}


@app.route('/api/ev_deadline')
def get_ev_deadline():
    """Return the EV deadline-charging plan summaries.

    Written by HeatpumpOptimizer.recalculate_ev_deadline_plans(). ``devices`` holds one
    summary per deadline-mode EV (scheduled or held back while the EV is not ready);
    ``primary`` repeats the first device so a Home Assistant REST sensor can read it
    without knowing the device name. ``primary`` is always an object.
    """
    with TinyDB('db.json') as db:
        doc = db.get(Query().id == 'ev_deadline')

    if doc:
        primary = doc.get('primary') or _EV_DEADLINE_EMPTY_PRIMARY
        return jsonify({
            'devices': doc.get('devices', {}),
            'primary': primary,
            'updated_at': doc.get('updated_at', '')
        })
    return jsonify({
        'devices': {},
        'primary': _EV_DEADLINE_EMPTY_PRIMARY,
        'updated_at': ''
    })

@app.route('/api/peak')
def get_peak():
    """Return current peak power consumption"""
    with TinyDB('db.json') as db:
        load_watcher_docs = db.search(Query().id == 'load_watcher')
    peak_calc_minutes = CONFIG.get('options', {}).get('peak_calculation_minutes', 15)
    
    if load_watcher_docs:
        data = load_watcher_docs[0]
        return jsonify({
            'peak_kw': data.get('current_peak_kw', 0.0),
            'timestamp': data.get('timestamp', ''),
            'total_energy_consumption': data.get('total_energy_consumption', 0.0),
            'max_peak_kw': data.get('max_peak_kw', 7.5),
            'available_power_kw': data.get('available_power_kw', 0.0),
            'slot_start': data.get('slot_start', ''),
            'slot_readings_count': data.get('slot_readings_count', 0),
            'peak_calculation_minutes': peak_calc_minutes
        })
    return jsonify({
        'peak_kw': 0.0,
        'timestamp': '',
        'total_energy_consumption': 0.0,
        'max_peak_kw': 7.5,
        'available_power_kw': 0.0,
        'slot_start': '',
        'slot_readings_count': 0,
        'peak_calculation_minutes': peak_calc_minutes
    })

@app.route('/api/device_limits')
def get_device_limits():
    """Return current device load limits"""
    with TinyDB('db.json') as db:
        limits_docs = db.search(Query().id == 'device_limitations')
    
    if limits_docs:
        return jsonify(limits_docs[0])
    return jsonify({
        'id': 'device_limitations',
        'timestamp': '',
        'available_power_watts': 0,
        'limits': {}
    })

@app.route('/api/predictions')
def get_predictions():
    """Return power usage and solar production predictions"""
    with TinyDB('db.json') as db:
        docs = db.search(Query().id == 'predictions')

    if docs:
        doc = docs[0]
        return jsonify({
            'id': doc.get('id', 'predictions'),
            'usage': doc.get('usage', []),
            'solar': doc.get('solar', []),
            'battery_soc': doc.get('battery_soc', {}),
            'ev_soc': doc.get('ev_soc', {}),
            'updated_at': doc.get('updated_at', '')
        })
    return jsonify({
        'id': 'predictions',
        'usage': [],
        'solar': [],
        'battery_soc': {},
        'ev_soc': {},
        'updated_at': ''
    })

@app.route('/api/gantt')
def get_gantt():
    """Generate and return Plotly chart: Gantt schedule, price histogram, and power predictions sharing one x-axis"""
    with TinyDB('db.json') as db:
        schedule_docs = db.search(Query().id == 'schedule')
        prediction_docs = db.search(Query().id == 'predictions')

    if not schedule_docs or not schedule_docs[0].get('schedule'):
        fig = go.Figure()
        fig.update_layout(title="No schedule data available")
        return fig.to_html(include_plotlyjs=False, full_html=False)

    schedule = schedule_docs[0]['schedule']
    original_battery_schedule = schedule_docs[0].get('original_battery_schedule', [])
    prices = schedule_docs[0].get('prices', [])
    horizon_start = schedule_docs[0].get('horizon_start')
    slot_minutes = schedule_docs[0].get('slot_minutes', 15)

    # Load predictions
    predictions = prediction_docs[0] if prediction_docs else {}
    usage_data = predictions.get('usage', [])
    solar_data = predictions.get('solar', [])
    battery_soc = predictions.get('battery_soc', {})
    ev_soc = predictions.get('ev_soc', {})
    thermal_temp = predictions.get('thermal_temp', {})  # {device: [{timestamp, temp}]} from the pre-heat LP
    thermal_temp_actual = predictions.get('thermal_temp_actual', {})  # {device: [{timestamp, temp}]} measured

    # Device labels and colors
    device_labels = {
        'wp': 'HP',  # Shortened for mobile
        'hw': 'HW',  # Shortened for mobile
        'battery_charge': 'Bat+',
        'battery_discharge': 'Bat-',
        'battery': 'Bat',
        'ev': 'EV',
        # Original planned battery times (for display only)
        'battery_charge_planned': 'Bat+ Plan',
        'battery_discharge_planned': 'Bat- Plan'
    }

    device_colors = {
        'HP': '#FF6B6B',
        'HW': '#4ECDC4',
        'Bat+': '#45B7D1',
        'Bat-': '#FFA07A',
        'Bat': '#95E1D3',
        'EV': '#9B59B6',
        # Lighter/dashed colors for planned (original) battery times
        'Bat+ Plan': '#A8D8E6',  # Lighter blue
        'Bat- Plan': '#FFD4B8'   # Lighter orange
    }

    # Prepare data for timeline
    df_list = []

    # First add original battery schedule (planned times) - these appear first/on top
    for entry in original_battery_schedule:
        device = entry.get('device')
        start = entry.get('start')
        stop = entry.get('stop')

        if device and start and stop:
            task_name = device_labels.get(device, device)
            df_list.append({
                'Task': task_name,
                'Start': pd.to_datetime(start),
                'Finish': pd.to_datetime(stop),
                'Type': 'Planned'
            })

    # Then add actual schedule entries
    for entry in schedule:
        device = entry.get('device')
        start = entry.get('start')
        stop = entry.get('stop')

        # Pre-heat heat-pump ranges carry a 'setpoint' and cover every slot, so as a Gantt bar they
        # are just an uninformative solid block. They are shown in the dedicated Heat Pump row
        # (commanded setpoint + predicted temperature) instead.
        if entry.get('setpoint') is not None:
            continue

        if device and start and stop:
            task_name = device_labels.get(device, device)
            df_list.append({
                'Task': task_name,
                'Start': pd.to_datetime(start),
                'Finish': pd.to_datetime(stop),
                'Type': 'Actual'
            })

    if not df_list:
        fig = go.Figure()
        fig.update_layout(title="No valid schedule entries")
        return fig.to_html(include_plotlyjs=False, full_html=False)

    df = pd.DataFrame(df_list)

    # Build prediction subplot title with totals
    total_usage = sum(r.get('predicted_kwh', 0) for r in usage_data)
    total_solar = sum(r.get('predicted_kwh', 0) for r in solar_data)
    parts = []
    if usage_data:
        parts.append(f"Usage: {total_usage:.1f} kWh")
    if solar_data:
        parts.append(f"Solar: {total_solar:.1f} kWh")
    pred_title = "Power Predictions" + (f" ({', '.join(parts)})" if parts else "")

    # Create the Gantt chart using px.timeline
    fig_gantt = px.timeline(
        df,
        x_start='Start',
        x_end='Finish',
        y='Task',
        color='Task',
        color_discrete_map=device_colors,
        hover_data={'Type': True}
    )

    # Set opacity based on Type (Planned vs Actual)
    for trace in fig_gantt.data:
        task_name = trace.name
        # Get the Type values for this task's bars
        task_df = df[df['Task'] == task_name]
        opacities = [0.5 if t == 'Planned' else 0.9 for t in task_df['Type']]
        trace.marker.opacity = opacities

    # Update hover template
    fig_gantt.update_traces(
        hovertemplate="<b>%{y}</b><br>Start: %{base|%Y-%m-%d %H:%M}<br>End: %{x|%Y-%m-%d %H:%M}<br>Type: %{customdata[0]}<extra></extra>"
    )

    # Layout rows: Gantt, Prices, (Heat Pump setpoint+temp — only when there is a pre-heat plan),
    # Predictions. The heat-pump row gets its own °C graph so it is clear both what setpoint is
    # commanded and how the room temperature responds.
    has_thermal = bool(thermal_temp) or any(e.get('setpoint') is not None for e in schedule)
    n_rows = 4 if has_thermal else 3
    hp_row = 3 if has_thermal else None
    pred_row = 4 if has_thermal else 3

    if has_thermal:
        row_heights = [0.30, 0.15, 0.22, 0.33]
        subplot_titles = ("Energy Optimization Schedule", "Electricity Prices (€/kWh)",
                          "Heat Pump — setpoint command, predicted & measured room temp (°C)", pred_title)
        specs = [[{}], [{}], [{}], [{'secondary_y': True}]]
    else:
        row_heights = [0.35, 0.20, 0.45]
        subplot_titles = ("Energy Optimization Schedule", "Electricity Prices (€/kWh)", pred_title)
        specs = [[{}], [{}], [{'secondary_y': True}]]

    fig = make_subplots(
        rows=n_rows, cols=1,
        row_heights=row_heights,
        shared_xaxes=True,
        vertical_spacing=0.05,
        subplot_titles=subplot_titles,
        specs=specs
    )

    # Add Gantt traces to row 1
    for trace in fig_gantt.data:
        trace.showlegend = False
        trace.width = 0.8  # Make bars thicker (0-1 range, where 1 is full row height)
        fig.add_trace(trace, row=1, col=1)

    # Copy y-axis category order from the gantt chart
    fig.update_yaxes(
        categoryorder=fig_gantt.layout.yaxis.categoryorder,
        categoryarray=fig_gantt.layout.yaxis.categoryarray,
        row=1, col=1
    )

    # Set x-axis type to date for proper rendering
    fig.update_xaxes(type='date', row=1, col=1)

    # Add "Now" line
    now = datetime.now()
    for row in range(1, n_rows + 1):
        fig.add_vline(
            x=now.timestamp() * 1000,
            line=dict(color="red", width=2, dash="dash"),
            row=row, col=1
        )
    fig.add_annotation(
        x=now,
        y=1,
        yref="y domain",
        text="Now",
        showarrow=False,
        yanchor="bottom",
        font=dict(color="red", size=10),
        row=1, col=1
    )

    # Add price histogram (row 2)
    if prices and horizon_start:
        # Create time slots for prices
        start_dt = datetime.fromisoformat(horizon_start)
        price_times = [start_dt + timedelta(minutes=slot_minutes * i) for i in range(len(prices))]

        # Convert prices from EUR/kWh to EUR cents/kWh for better readability
        prices_cents = [p * 100 for p in prices]

        fig.add_trace(
            go.Bar(
                x=price_times,
                y=prices_cents,
                name='Price',
                marker=dict(
                    color=prices_cents,
                    colorscale='RdYlGn_r',  # Red for high, green for low
                    showscale=True,
                    colorbar=dict(
                        title="€ct/kWh",
                        len=0.18,
                        y=0.54,
                        yanchor='middle'
                    )
                ),
                hovertemplate='<b>Time:</b> %{x}<br><b>Price:</b> %{y:.2f} €ct/kWh<extra></extra>',
                showlegend=False
            ),
            row=2, col=1
        )

    # Heat-pump row: the commanded quick-veto setpoint (exactly what the addon writes to the heat
    # pump, drawn as a step so every change is visible) and the resulting predicted room
    # temperature, both in °C on the same axis.
    if has_thermal:
        # Commanded setpoint per device, reconstructed from the schedule entries that carry a
        # 'setpoint'. Each range contributes a flat segment at its setpoint value; the step shape
        # draws the jump to the next range.
        sp_by_device = {}
        for entry in schedule:
            if entry.get('setpoint') is None:
                continue
            sp_by_device.setdefault(entry['device'], []).append(entry)
        setpoint_colors = ['#3949AB', '#283593', '#1A237E']
        for i, (name, entries) in enumerate(sorted(sp_by_device.items())):
            entries = sorted(entries, key=lambda e: e['start'])
            xs, ys = [], []
            for e in entries:
                xs.append(pd.to_datetime(e['start'])); ys.append(e['setpoint'])
                xs.append(pd.to_datetime(e['stop'])); ys.append(e['setpoint'])
            label = f"HP setpoint command{' ' + name if len(sp_by_device) > 1 else ''} (°C)"
            fig.add_trace(
                go.Scatter(
                    x=xs, y=ys, name=label, mode='lines',
                    line=dict(color=setpoint_colors[i % len(setpoint_colors)], width=2.5, shape='hv'),
                    hovertemplate='Set to <b>%{y:.1f} °C</b><extra>' + label + '</extra>',
                    showlegend=True
                ),
                row=hp_row, col=1
            )
        # Predicted room temperature per device.
        temp_colors = ['#D81B60', '#AD1457', '#880E4F']
        for i, (name, temp_entries) in enumerate(thermal_temp.items()):
            if not temp_entries:
                continue
            label = f"Predicted room temp{' ' + name if len(thermal_temp) > 1 else ''} (°C)"
            fig.add_trace(
                go.Scatter(
                    x=[r['timestamp'] for r in temp_entries],
                    y=[r['temp'] for r in temp_entries],
                    name=label,
                    mode='lines',
                    line=dict(color=temp_colors[i % len(temp_colors)], width=2, shape='spline'),
                    hovertemplate='%{y:.1f} °C<extra>' + label + '</extra>',
                    showlegend=True
                ),
                row=hp_row, col=1
            )
        # Measured room temperature per device (actual, from HA history + live readings), so the
        # predicted trajectory can be checked against what the house actually did.
        actual_colors = ['#00695C', '#004D40', '#26A69A']
        for i, (name, temp_entries) in enumerate(thermal_temp_actual.items()):
            if not temp_entries:
                continue
            label = f"Measured room temp{' ' + name if len(thermal_temp_actual) > 1 else ''} (°C)"
            color = actual_colors[i % len(actual_colors)]
            fig.add_trace(
                go.Scatter(
                    x=[r['timestamp'] for r in temp_entries],
                    y=[r['temp'] for r in temp_entries],
                    name=label,
                    # lines+markers so the current/measured temperature is visible even when only a
                    # point or two exist (e.g. just after a restart, early in the horizon).
                    mode='lines+markers',
                    line=dict(color=color, width=2.5),
                    marker=dict(color=color, size=6),
                    hovertemplate='%{y:.2f} °C<extra>' + label + '</extra>',
                    showlegend=True
                ),
                row=hp_row, col=1
            )
        fig.update_yaxes(title_text='°C', row=hp_row, col=1)

    # Add power prediction traces (predictions row)
    soc_colors = ['#45B7D1', '#2E86AB', '#1B5E7B']

    if usage_data:
        fig.add_trace(
            go.Scatter(
                x=[r['timestamp'] for r in usage_data],
                y=[r['predicted_kwh'] for r in usage_data],
                name='Predicted Usage (kWh)',
                mode='lines',
                line=dict(color='#FF6B6B', width=2, shape='spline'),
                hovertemplate='%{y:.3f} kWh<extra>Predicted Usage</extra>',
                showlegend=True
            ),
            row=pred_row, col=1, secondary_y=False
        )

    if solar_data:
        fig.add_trace(
            go.Scatter(
                x=[r['timestamp'] for r in solar_data],
                y=[r['predicted_kwh'] for r in solar_data],
                name='Predicted Solar (kWh)',
                mode='lines',
                line=dict(color='#FFA726', width=2, shape='spline'),
                hovertemplate='%{y:.3f} kWh<extra>Predicted Solar</extra>',
                showlegend=True
            ),
            row=pred_row, col=1, secondary_y=False
        )

    for i, (name, soc_entries) in enumerate(battery_soc.items()):
        if not soc_entries:
            continue
        label = f"Battery SOC{' ' + name if len(battery_soc) > 1 else ''} (%)"
        fig.add_trace(
            go.Scatter(
                x=[r['timestamp'] for r in soc_entries],
                y=[r['soc_percent'] for r in soc_entries],
                name=label,
                mode='lines',
                line=dict(color=soc_colors[i % len(soc_colors)], width=2, shape='spline'),
                hovertemplate='%{y:.1f}%<extra>' + label + '</extra>',
                showlegend=True
            ),
            row=pred_row, col=1, secondary_y=True
        )

    ev_soc_colors = ['#9B59B6', '#6C3483', '#C39BD3']
    for i, (name, soc_entries) in enumerate(ev_soc.items()):
        if not soc_entries:
            continue
        label = f"EV SOC{' ' + name if len(ev_soc) > 1 else ''} (%)"
        fig.add_trace(
            go.Scatter(
                x=[r['timestamp'] for r in soc_entries],
                y=[r['soc_percent'] for r in soc_entries],
                name=label,
                mode='lines',
                line=dict(color=ev_soc_colors[i % len(ev_soc_colors)], width=2, shape='spline', dash='dot'),
                hovertemplate='%{y:.1f}%<extra>' + label + '</extra>',
                showlegend=True
            ),
            row=pred_row, col=1, secondary_y=True
        )

    # Configure y-axes for prediction subplot
    fig.update_yaxes(title_text='kWh', rangemode='tozero', row=pred_row, col=1, secondary_y=False)
    fig.update_yaxes(title_text='SOC (%)', range=[0, 100], showgrid=False, row=pred_row, col=1, secondary_y=True)

    # Update layout
    fig.update_layout(
        height=1150 if has_thermal else 950,
        showlegend=True,
        legend=dict(
            orientation='h',
            x=0,
            y=-0.05,
            xanchor='left',
            yanchor='top'
        ),
        margin=dict(l=50, r=60, t=60, b=100),
        autosize=True,
        dragmode=False,  # Disable panning
        modebar=dict(
            orientation='v',
            bgcolor='rgba(255,255,255,0.7)'
        ),
        font=dict(size=10),
        hovermode='closest'  # Use closest for individual tooltips
    )

    # Update x-axis for all subplots with spike line
    fig.update_xaxes(
        tickformat='%Y-%m-%d %H:%M',
        tickangle=-45,
        showspikes=True,
        spikemode='across+toaxis',
        spikesnap='cursor',
        spikecolor='rgba(100, 100, 100, 0.5)',
        spikethickness=1,
        spikedash='dot'
    )

    # Add title to bottom x-axis (row 3)
    fig.update_xaxes(title_text="Time", row=pred_row, col=1)

    # Update y-axis for price histogram
    fig.update_yaxes(title_text="€ct/kWh", row=2, col=1)

    # Configure for better mobile experience
    config = {
        'responsive': True,
        'displayModeBar': True,
        'modeBarButtonsToRemove': ['select2d', 'lasso2d', 'autoScale2d', 'pan2d', 'zoom2d', 'zoomIn2d', 'zoomOut2d'],
        'scrollZoom': False,  # Disable scroll zoom
        'displaylogo': False
    }

    # Generate HTML and add crosshair cursor
    html_output = fig.to_html(include_plotlyjs=False, full_html=False, config=config)
    html_output = html_output.replace('<div', '<div style="cursor: crosshair;"', 1)

    return html_output



# Generic route to download any file from /data
@app.route('/download/<path:filename>')
def download_file(filename):
    # Explicitly set mimetype for .csv files
    if filename.endswith('.csv'):
        return send_from_directory('/app', filename, as_attachment=True, mimetype='text/csv')
    return send_from_directory('/app', filename, as_attachment=True)

def run_server():
    """Run the Flask server"""
    app.run(host='0.0.0.0', port=8099, debug=True, use_reloader=False)