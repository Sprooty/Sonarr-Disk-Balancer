# Sonarr-Disk-Balancer# Project Documentation

## Overview

This Python script is designed to manage TV series storage by interfacing with a Sonarr API. It optimizes disk space usage by moving TV series between disks based on free space predictions. The script performs a variety of functions including fetching series and disk space information, recommending moves, executing those moves, and logging all actions.

## Configuration

The script is configured through a `config.json` file which contains several parameters that determine its behavior:

### Config Parameters

- `SONARR_API_URL`: The URL to your Sonarr API server.
- `SONARR_API_KEY`: Your API key for accessing Sonarr.
- `DEBUG`: Boolean flag to turn on detailed logging for debugging purposes.
- `max_moves`: Maximum number of series moves the script should perform in one execution.
- `dry_run`: Boolean flag for simulating moves without actually making changes to test the script's decision-making.
- `timeout_seconds`: Maximum time in seconds for the script to wait for Sonarr to log a successful move.
- `valid_root_paths`: List of root paths that are valid targets for moving series to manage disk space efficiently.

## Script Functionality

### Logging Setup
- The script includes comprehensive logging capabilities that capture both detailed debug information and general operational logs. Logs are outputted to both the console and a file to facilitate debugging and monitoring.

### API Interaction
- The script interacts with the Sonarr API to fetch data on TV series and disk spaces. This includes retrieving current storage locations, series file sizes, and available disk space.

### Series Movement Operations
- Core to the script's functionality is the ability to move series between different storage paths based on various criteria such as disk space availability and previous move history. This ensures optimal use of disk space.

### State Management
- It maintains a state of operations which allows it to resume interrupted tasks, track previous moves, and avoid unnecessary or repetitive operations.

### Decision Making and Recommendations
- Based on the current state of disk space and series placement, the script generates recommendations for moving series to optimize disk usage. These recommendations take into account past activities to avoid counterproductive changes.

### Historical Data Handling
- All moves are recorded in a historical log to prevent the script from making the same move repeatedly within a short time frame, thus optimizing the decision-making process over time.

### Reporting
- The script can report on current and predicted disk space usage without actually moving files. This feature is useful for assessing potential optimization outcomes before executing changes.

### Execution Control
- Operations can be performed in a real or simulated mode, allowing for testing and verification of the script's decision logic without impacting the actual system.

### Monitoring and Auditing
- Post-move operations include monitoring of Sonarr's logs to confirm successful file moves and updating the internal state to reflect these changes. This ensures the system's accuracy and reliability.

### Error Handling
- Comprehensive error handling mechanisms are in place to manage API interaction failures, logging issues, and unexpected script behavior, which helps maintain stability and usability.


## Execution Flow

1. Fetches series and disk space data from Sonarr.
2. Saves current move history to a file.
3. Reports on disk space both currently and predicted after hypothetical moves.
4. Performs actual disk space balancing based on configured parameters.