FROM python:3.12-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y jq glpk-utils && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
# Install aiohttp and aiodns with compatible versions first to avoid conflicts
RUN pip install 'aiohttp>=3.9.0' 'aiodns>=3.1.0'
RUN pip install homeassistant requests apscheduler pulp flask flask-cors tinydb pandas lightgbm numpy scikit-learn websockets plotly entsoe-py pydantic pydantic-settings

# Copy source files
COPY src /app/src
COPY web /app/web
COPY optimization_plan.py /app/optimization_plan.py
COPY config.json /app/config.json
COPY run.sh /app/run.sh

# Make the entrypoint script executable
RUN chmod +x /app/run.sh

# Expose port for web UI
EXPOSE 8099

# Entrypoint
ENTRYPOINT ["/app/run.sh"]