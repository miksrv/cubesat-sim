#!/bin/bash

# Checks
if [ ! -f "requirements.txt" ]; then
    echo "Error: requirements.txt not found!"
    exit 1
fi

# Step 1: Create log directory
LOG_DIR="/var/log/cubesat"
sudo mkdir -p $LOG_DIR
echo "Log directory $LOG_DIR created"

# Step 2: Set permissions for current user
CURRENT_USER=$(whoami)
sudo chown -R $CURRENT_USER:$CURRENT_USER $LOG_DIR
sudo chmod -R 755 $LOG_DIR
echo "Permissions set for user $CURRENT_USER on $LOG_DIR"

# Step 3: Create virtual environment
VENV_DIR="./venv"
if [ ! -d "$VENV_DIR" ]; then
    python3 -m venv $VENV_DIR
    echo "Virtual environment created in $VENV_DIR"
else
    echo "Virtual environment already exists"
fi

# Step 4: Install dependencies
source $VENV_DIR/bin/activate
pip install -r requirements.txt
deactivate
echo "Dependencies installed"

# Step 5: Copy .service files to /etc/systemd/system/
SERVICE_DIR="/etc/systemd/system"
sudo cp ./systemd/cubesat-adcs.service $SERVICE_DIR/
sudo cp ./systemd/cubesat-obc.service $SERVICE_DIR/
sudo cp ./systemd/cubesat-eps.service $SERVICE_DIR/
sudo cp ./systemd/cubesat-payload.service $SERVICE_DIR/
sudo cp ./systemd/cubesat-telemetry.service $SERVICE_DIR/
echo "Service files copied to $SERVICE_DIR"

# Replace %i with actual user in .service files (if needed; systemd usually does this, but just in case)
sudo sed -i "s/%i/$CURRENT_USER/g" $SERVICE_DIR/cubesat-*.service

# Step 6: Reload systemd and start/enable services
sudo systemctl daemon-reload
for service in cubesat-obc cubesat-payload cubesat-telemetry; do
    sudo systemctl enable $service.service
    sudo systemctl start $service.service
    sudo systemctl status $service.service --no-pager
done

echo "Installation complete! Check logs: journalctl -u cubesat-obc.service -f"
