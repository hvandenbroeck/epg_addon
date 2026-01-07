from flask import Flask, render_template, jsonify, send_from_directory
from flask_cors import CORS
import json
from datetime import datetime, timedelta
import logging
from tinydb import TinyDB, Query
import plotly.express as px
import plotly.graph_objects as go
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

@app.route('/api/gantt')
def get_gantt():
    """Generate and return Plotly Gantt chart as interactive HTML"""
    with TinyDB('db.json') as db:
        schedule_docs = db.search(Query().id == 'schedule')
    
    if not schedule_docs or not schedule_docs[0].get('schedule'):
        fig = go.Figure()
        fig.update_layout(title="No schedule data available")
        return fig.to_html(include_plotlyjs=True, full_html=False)
    
    schedule = schedule_docs[0]['schedule']
    original_battery_schedule = schedule_docs[0].get('original_battery_schedule', [])
    
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
    
    # Create timeline using Plotly Express
    fig = px.timeline(df, x_start="Start", x_end="Finish", y="Task", color="Task",
                      color_discrete_map=device_colors,
                      title="Energy Optimization Schedule")
    
    # Add "Now" line as a shape
    now = datetime.now()
    fig.add_shape(
        type="line",
        x0=now, x1=now,
        y0=0, y1=1,
        yref="paper",
        line=dict(color="red", width=2, dash="dash")
    )
    fig.add_annotation(
        x=now, y=1, yref="paper",
        text="Now",
        showarrow=False,
        yanchor="bottom",
        font=dict(color="red", size=12)
    )
    
    # Update layout
    fig.update_layout(
        xaxis_title="Time",
        yaxis_title="",  # Remove y-axis title to save space
        height=400,
        showlegend=False,
        xaxis=dict(tickformat='%Y-%m-%d %H:%M', tickangle=-45),
        margin=dict(l=50, r=20, t=50, b=80),  # Reduced left margin significantly
        # Mobile-friendly settings
        autosize=True,
        dragmode='pan',
        modebar=dict(
            orientation='v',
            bgcolor='rgba(255,255,255,0.7)'
        ),
        font=dict(size=10)  # Smaller font for mobile
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