FROM python:3.9-slim

# Set working directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y jq glpk-utils && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
RUN pip install homeassistant requests apscheduler pulp flask flask-cors tinydb pandas lightgbm numpy scikit-learn websockets

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