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
    """Return battery price thresholds"""
    with TinyDB('db.json') as db:
        schedule_docs = db.search(Query().id == 'schedule')
    
    if schedule_docs and 'battery_price_thresholds' in schedule_docs[0]:
        thresholds = schedule_docs[0]['battery_price_thresholds']
        updated_at = schedule_docs[0].get('updated_at', '')
        solar_only_mode = schedule_docs[0].get('solar_only_mode', False)
        last_soc_recalc = schedule_docs[0].get('last_soc_recalc', '')
        return jsonify({
            'max_charge_price': thresholds.get('max_charge_price'),
            'min_discharge_price': thresholds.get('min_discharge_price'),
            'price_history_days': thresholds.get('price_history_days'),
            'charge_percentile': thresholds.get('charge_percentile'),
            'discharge_percentile': thresholds.get('discharge_percentile'),
            'price_diff_threshold': thresholds.get('price_diff_threshold'),
            'updated_at': updated_at,
            'solar_only_mode': solar_only_mode,
            'last_soc_recalc': last_soc_recalc
        })
    return jsonify({
        'max_charge_price': None,
        'min_discharge_price': None,
        'price_history_days': None,
        'charge_percentile': None,
        'discharge_percentile': None,
        'price_diff_threshold': None,
        'updated_at': '',
        'solar_only_mode': False,
        'last_soc_recalc': ''
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
            'updated_at': doc.get('updated_at', '')
        })
    return jsonify({
        'id': 'predictions',
        'usage': [],
        'solar': [],
        'battery_soc': {},
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
    battery_discharge_min_soc = schedule_docs[0].get('battery_discharge_min_soc', {})

    # Load predictions
    predictions = prediction_docs[0] if prediction_docs else {}
    usage_data = predictions.get('usage', [])
    solar_data = predictions.get('solar', [])
    battery_soc = predictions.get('battery_soc', {})

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

    # Create subplots: Gantt (row 1), Prices (row 2), Predictions (row 3)
    fig = make_subplots(
        rows=3, cols=1,
        row_heights=[0.35, 0.20, 0.45],
        shared_xaxes=True,
        vertical_spacing=0.06,
        subplot_titles=("Energy Optimization Schedule", "Electricity Prices (€/kWh)", pred_title),
        specs=[[{}], [{}], [{'secondary_y': True}]]
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
    for row in [1, 2, 3]:
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

    # Add power prediction traces (row 3)
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
            row=3, col=1, secondary_y=False
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
            row=3, col=1, secondary_y=False
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
            row=3, col=1, secondary_y=True
        )

    # Add a min SOC step-function trace for each battery device that has discharge min SOC data
    min_soc_colors = ['#E74C3C', '#C0392B', '#922B21']
    for j, (dev_name, iso_min_soc_map) in enumerate(battery_discharge_min_soc.items()):
        if not iso_min_soc_map:
            continue
        # Build step function: for each discharge schedule entry belonging to this device,
        # emit a horizontal segment at the min_soc_percent for its duration.
        discharge_entries = [
            e for e in schedule
            if e.get('device') == f"{dev_name}_discharge"
        ]
        step_x = []
        step_y = []
        for entry in sorted(discharge_entries, key=lambda e: e['start']):
            entry_start = entry['start']
            entry_stop = entry['stop']
            soc_val = iso_min_soc_map.get(entry_start)
            if soc_val is None:
                continue
            step_x.extend([pd.to_datetime(entry_start), pd.to_datetime(entry_stop)])
            step_y.extend([soc_val, soc_val])
        if step_x:
            min_soc_label = f"Min SOC {dev_name} (%)" if len(battery_discharge_min_soc) > 1 else "Min SOC (%)"
            fig.add_trace(
                go.Scatter(
                    x=step_x,
                    y=step_y,
                    name=min_soc_label,
                    mode='lines',
                    line=dict(
                        color=min_soc_colors[j % len(min_soc_colors)],
                        width=2,
                        dash='dot',
                        shape='hv',
                    ),
                    hovertemplate='%{y:.1f}%<extra>' + min_soc_label + '</extra>',
                    showlegend=True,
                ),
                row=3, col=1, secondary_y=True
            )

    # Configure y-axes for prediction subplot
    fig.update_yaxes(title_text='kWh', rangemode='tozero', row=3, col=1, secondary_y=False)
    fig.update_yaxes(title_text='SOC (%)', range=[0, 100], showgrid=False, row=3, col=1, secondary_y=True)

    # Update layout
    fig.update_layout(
        height=950,
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
    fig.update_xaxes(title_text="Time", row=3, col=1)

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