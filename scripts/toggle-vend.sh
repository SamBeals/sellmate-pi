#!/bin/bash

if systemctl is-active --quiet sellmate-poller.service; then
    echo "Stopping poller..."
    sudo systemctl stop sellmate-poller.service
else
    echo "Starting poller..."
    sudo systemctl start sellmate-poller.service
fi
