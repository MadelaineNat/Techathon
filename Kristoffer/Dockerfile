FROM python:3.12-slim

# Don't write .pyc files, and don't buffer stdout (so `docker logs` shows prints immediately)
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install dependencies first so Docker can cache this layer
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy the scripts (ble_scanner2.py, ex-usage.py, ...)
COPY . .

# bleak talks to the HOST's bluetoothd over the system D-Bus socket,
# so BlueZ is not installed here. See the run command for the mounts.
CMD ["python", "ex-usage.py"]