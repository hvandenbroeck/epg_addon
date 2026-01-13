from flask import Flask, render_template, jsonify, send_from_directory, request
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
        return jsonify({
            'max_charge_price': thresholds.get('max_charge_price'),
            'min_discharge_price': thresholds.get('min_discharge_price'),
            'price_history_days': thresholds.get('price_history_days'),
            'charge_percentile': thresholds.get('charge_percentile'),
            'discharge_percentile': thresholds.get('discharge_percentile'),
            'price_diff_threshold': thresholds.get('price_diff_threshold'),
            'updated_at': updated_at
        })
    return jsonify({
        'max_charge_price': None,
        'min_discharge_price': None,
        'price_history_days': None,
        'charge_percentile': None,
        'discharge_percentile': None,
        'price_diff_threshold': None,
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

@app.route('/api/logs')
def get_logs():
    """Return logs with optional filtering"""
    # Get query parameters
    level = request.args.get('level', None)  # DEBUG, INFO, WARNING, ERROR, CRITICAL
    module = request.args.get('module', None)  # Filter by module/filename
    limit = request.args.get('limit', 500, type=int)  # Default to last 500 logs
    offset = request.args.get('offset', 0, type=int)  # For pagination
    
    with TinyDB('logs.json') as db:
        logs_table = db.table('logs')
        
        # Build query
        LogQuery = Query()
        conditions = []
        
        if level:
            conditions.append(LogQuery.level == level.upper())
        
        if module:
            # Support filtering by module/filename
            conditions.append(
                (LogQuery.module == module) | 
                (LogQuery.filename == module) | 
                (LogQuery.filename == f"{module}.py")
            )
        
        # Execute query
        if conditions:
            # Combine all conditions with AND
            query = conditions[0]
            for condition in conditions[1:]:
                query = query & condition
            logs = logs_table.search(query)
        else:
            logs = logs_table.all()
        
        # Sort by timestamp (newest first)
        logs.sort(key=lambda x: x.get('timestamp', ''), reverse=True)
        
        # Get unique modules for filter dropdown
        unique_modules = sorted(set(log.get('module', '') for log in logs_table.all()))
        
        # Apply pagination
        total = len(logs)
        logs = logs[offset:offset + limit]
        
        return jsonify({
            'logs': logs,
            'total': total,
            'offset': offset,
            'limit': limit,
            'unique_modules': unique_modules
        })

@app.route('/api/logs/clear', methods=['POST'])
def clear_logs():
    """Clear all logs from the database"""
    with TinyDB('logs.json') as db:
        logs_table = db.table('logs')
        logs_table.truncate()
    return jsonify({'status': 'success', 'message': 'All logs cleared'})

@app.route('/api/gantt')
def get_gantt():
    """Generate and return Plotly Gantt chart with price histogram as interactive HTML"""
    with TinyDB('db.json') as db:
        schedule_docs = db.search(Query().id == 'schedule')
    
    if not schedule_docs or not schedule_docs[0].get('schedule'):
        fig = go.Figure()
        fig.update_layout(title="No schedule data available")
        return fig.to_html(include_plotlyjs=True, full_html=False)
    
    schedule = schedule_docs[0]['schedule']
    original_battery_schedule = schedule_docs[0].get('original_battery_schedule', [])
    prices = schedule_docs[0].get('prices', [])
    horizon_start = schedule_docs[0].get('horizon_start')
    slot_minutes = schedule_docs[0].get('slot_minutes', 15)
    
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
        return fig.to_html(include_plotlyjs=True, full_html=False)
    
    df = pd.DataFrame(df_list)
    
    # Create subplots: Gantt chart on top, price histogram below
    fig = make_subplots(
        rows=2, cols=1,
        row_heights=[0.6, 0.4],  # Gantt takes 60%, histogram 40%
        shared_xaxes=True,
        vertical_spacing=0.08,
        subplot_titles=("Energy Optimization Schedule", "Electricity Prices (€/kWh)")
    )
    
    # Add Gantt chart traces (subplot 1)
    # We need to manually create the Gantt bars from the dataframe
    for idx, row in df.iterrows():
        task = row['Task']
        color = device_colors.get(task, '#999999')
        
        # Determine opacity based on type (planned vs actual)
        opacity = 0.5 if row['Type'] == 'Planned' else 0.9
        
        fig.add_trace(
            go.Scatter(
                x=[row['Start'], row['Finish'], row['Finish'], row['Start'], row['Start']],
                y=[task, task, task, task, task],
                fill='toself',
                fillcolor=color,
                line=dict(color=color, width=30),
                opacity=opacity,
                mode='lines',
                showlegend=False,
                hovertemplate=f"<b>{task}</b><br>Start: %{{x[0]}}<br>End: %{{x[1]}}<extra></extra>"
            ),
            row=1, col=1
        )
    
    # Add "Now" line to Gantt chart
    now = datetime.now()
    fig.add_vline(
        x=now.timestamp() * 1000,  # Convert to milliseconds
        line=dict(color="red", width=2, dash="dash"),
        row=1, col=1
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
    
    # Add price histogram (subplot 2)
    if prices and horizon_start:
        # Create time slots for prices
        start_dt = datetime.fromisoformat(horizon_start)
        price_times = [start_dt + timedelta(minutes=slot_minutes * i) for i in range(len(prices))]
        
        # Convert prices from EUR/kWh to EUR cents/kWh for better readability
        prices_cents = [p * 100 for p in prices]
        
        # Create bar chart for prices
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
                        len=0.3,
                        y=0.15,
                        yanchor='middle'
                    )
                ),
                hovertemplate='<b>Time:</b> %{x}<br><b>Price:</b> %{y:.2f} €ct/kWh<extra></extra>',
                showlegend=False
            ),
            row=2, col=1
        )
        
        # Add "Now" line to price chart
        fig.add_vline(
            x=now.timestamp() * 1000,
            line=dict(color="red", width=2, dash="dash"),
            row=2, col=1
        )
    
    # Update layout
    fig.update_layout(
        height=700,  # Increased height for two subplots
        showlegend=False,
        margin=dict(l=50, r=20, t=60, b=60),
        autosize=True,
        dragmode='pan',
        modebar=dict(
            orientation='v',
            bgcolor='rgba(255,255,255,0.7)'
        ),
        font=dict(size=10),
        hovermode='closest'
    )
    
    # Update x-axis for both subplots
    fig.update_xaxes(
        tickformat='%Y-%m-%d %H:%M',
        tickangle=-45,
        row=2, col=1,
        title_text="Time"
    )
    
    # Update y-axis for Gantt chart
    fig.update_yaxes(
        title_text="",
        row=1, col=1
    )
    
    # Update y-axis for price histogram
    fig.update_yaxes(
        title_text="€ct/kWh",
        row=2, col=1
    )
    
    # Configure for better mobile experience
    config = {
        'responsive': True,
        'displayModeBar': True,
        'modeBarButtonsToRemove': ['select2d', 'lasso2d', 'autoScale2d'],
        'modeBarButtonsToAdd': ['pan2d'],
        'scrollZoom': True,
        'displaylogo': False
    }
    
    return fig.to_html(include_plotlyjs=True, full_html=False, config=config)



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