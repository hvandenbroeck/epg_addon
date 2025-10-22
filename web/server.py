from flask import Flask, render_template, jsonify
from flask_cors import CORS
import json
from datetime import datetime, timedelta
import logging
from tinydb import TinyDB, Query

logger = logging.getLogger(__name__)

app = Flask(__name__)
CORS(app)


@app.route('/')
def index():
    """Render main page"""
    return render_template('index.html')

@app.route('/api/results')
def get_results():
    """Return optimizer results as JSON"""
    db = TinyDB('db.json')
    schedule_docs = db.search(Query().id == 'schedule')
    return schedule_docs

def run_server():
    """Run the Flask server"""
    app.run(host='0.0.0.0', port=8099, debug=False, use_reloader=False)