import requests
import json
import pandas as pd
import logging
import sys
import os
from datetime import datetime, timedelta, timezone
import time
import re

# Load configuration from config.json
with open("config.json", "r") as config_file:
    config = json.load(config_file)

# Assign config values to variables
SONARR_API_URL = config['SONARR_API_URL']
SONARR_API_KEY = config['SONARR_API_KEY']
DEBUG = config['DEBUG']
max_moves = config['max_moves']
dry_run = config['dry_run']
timeout = config['timeout_seconds']  
valid_root_paths = [path.rstrip('/').lower() for path in config['valid_root_paths']]


# Setup logging to both file and console with UTF-8 encoding
logger = logging.getLogger()
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Create file handler to log to file
file_handler = logging.FileHandler('logfile.log', encoding='utf-8')
file_handler.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Create console handler to log to console
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Create formatter and add it to the handlers
formatter = logging.Formatter('%(asctime)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add the handlers to the logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

STATE_FILE = 'move_state.json'
MOVE_HISTORY_FILE = 'move_history.json'

def get_free_space_via_api():
    """Get free space for all monitored paths via Sonarr API."""
    disk_space_endpoint = f"{SONARR_API_URL}/diskspace"
    headers = {"X-Api-Key": SONARR_API_KEY}
    
    response = requests.get(disk_space_endpoint, headers=headers)
    response.raise_for_status()
    return response.json()

def get_series_info():
    """Get series information from Sonarr API."""
    series_endpoint = f"{SONARR_API_URL}/series"
    headers = {"X-Api-Key": SONARR_API_KEY}
    
    series_data = []
    
    try:
        series_response = requests.get(series_endpoint, headers=headers)
        series_response.raise_for_status()
        series_list = series_response.json()
        
        for series in series_list:
            series_id = series['id']
            title = series['title']
            path = series['path']
            root_folder_path = series['rootFolderPath'].rstrip('/').lower()
            
            if root_folder_path in valid_root_paths:
                episode_files_endpoint = f"{SONARR_API_URL}/episodefile?seriesId={series_id}"
                episode_files_response = requests.get(episode_files_endpoint, headers=headers)
                episode_files_response.raise_for_status()
                episode_files = episode_files_response.json()
                
                total_size_bytes = sum(episode_file['size'] for episode_file in episode_files)
                
                if total_size_bytes > 0:
                    series_data.append({
                        'series_id': series_id,
                        'title': title,
                        'path': path,
                        'root_folder_path': root_folder_path,
                        'total_size_bytes': total_size_bytes
                    })
        
        series_df = pd.DataFrame(series_data)
        series_df['total_size_gb'] = series_df['total_size_bytes'] / (1024 ** 3)
        return series_df
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error querying Sonarr API: {e}")
        return pd.DataFrame()

def load_state(file_path):
    """Load the state from a file."""
    if os.path.exists(file_path):
        with open(file_path, 'r') as f:
            return json.load(f)
    return {}

def save_state(state, file_path):
    """Save the state to a file."""
    with open(file_path, 'w') as f:
        json.dump(state, f)

def move_series(series_id, new_root_path, dry_run=False):
    """Move a series to a new path in Sonarr."""
    series_endpoint = f"{SONARR_API_URL}/series/{series_id}"
    headers = {
        "X-Api-Key": SONARR_API_KEY,
        "Content-Type": "application/json"
    }
    
    try:
        logger.info(f"Fetching current series information for Series ID: {series_id}...")
        response = requests.get(series_endpoint, headers=headers)
        logger.debug(f"GET {series_endpoint} - Status Code: {response.status_code}")
        response.raise_for_status()
        series_info = response.json()
        old_path = series_info['path']
        new_path = f"{new_root_path}/{os.path.basename(old_path)}"
        series_info['path'] = new_path
        move_files = True
        
        if dry_run:
            logger.info(f"Dry-run: Would move series '{series_info['title']}' from {old_path} to {new_path}")
            return True
        
        logger.info(f"Updating series path from {old_path} to {new_path} and moving files...")
        update_response = requests.put(series_endpoint, headers=headers, json=series_info, params={"moveFiles": move_files})
        logger.debug(f"PUT {series_endpoint} - Status Code: {update_response.status_code}")
        update_response.raise_for_status()
        logger.info(f"Series config updated successfully. New path: {series_info['path']}")
        logger.info(f"Sonarr will now move the files, this part may take minutes, hours, days depending how big the series is.")
        
        command_endpoint = f"{SONARR_API_URL}/command"
        rescan_command = {
            "name": "RescanSeries",
            "seriesId": series_id
        }
        logger.debug(f"Triggering rescan for Series ID: {series_id}...")
        rescan_response = requests.post(command_endpoint, headers=headers, json=rescan_command)
        logger.debug(f"POST {command_endpoint} - Status Code: {rescan_response.status_code}")
        rescan_response.raise_for_status()
        logger.info(f"Monitoring Sonarr Log for completion of file move")
        return True
    
    except requests.exceptions.RequestException as e:
        logger.error(f"Error moving series: {e}")
        return False

def update_move_history(series_id, new_root_path, title):
    """Update move history to avoid ping-ponging."""
    move_history = load_state(MOVE_HISTORY_FILE)
    move_history[str(series_id)] = {
        "title": title,
        "last_moved_to": new_root_path,
        "timestamp": datetime.now().isoformat()
    }
    save_state(move_history, MOVE_HISTORY_FILE)


def should_move_series(series_id, new_root_path, cooldown_days=7):
    """Determine if the series should be moved, considering history."""
    move_history = load_state(MOVE_HISTORY_FILE)
    if str(series_id) in move_history:
        last_move = move_history[str(series_id)]
        if last_move['last_moved_to'] == new_root_path:
            logger.info(f"Skipping move for Series ID: {series_id} as it was recently moved to {new_root_path}")
            return False
        last_move_date = datetime.fromisoformat(last_move['timestamp'])
        if datetime.now() - last_move_date < timedelta(days=cooldown_days):
            logger.info(f"Skipping move for Series ID: {series_id} due to cooldown period.")
            return False
    return True

def perform_moves(recommendations, max_moves, dry_run=False):
    """Perform the recommended moves."""
    state = load_state(STATE_FILE)
    moves_completed = 0
    
    for rec in recommendations:
        if moves_completed >= max_moves:
            break
        
        series_id = rec['series_id']
        new_root_path = rec['recommended_root']
        current_path = rec['path']
        title = rec['title']  # Get the title from the recommendation

        if str(series_id) in state:
            continue
        
        if should_move_series(series_id, new_root_path):
            success = move_series(series_id, new_root_path, dry_run)
            if success:
                state[str(series_id)] = new_root_path
                save_state(state, STATE_FILE)
                update_move_history(series_id, new_root_path, title)  # Pass the title to the function
                moves_completed += 1

                expected_path = f"{new_root_path}/{os.path.basename(current_path)}"
                monitor_sonarr_logs(series_id, expected_path)
    
    logger.info(f"Completed {moves_completed} moves out of {max_moves} requested. The script will now exit.")

def save_move_history_to_file():
    """Save move history to a file."""
    if os.path.exists(MOVE_HISTORY_FILE):
        with open(MOVE_HISTORY_FILE, 'r', encoding='utf-8') as f:
            move_history = json.load(f)
        
        # Save move history to its own file
        with open('move_history.txt', 'w', encoding='utf-8') as f:
            f.write("Move History:\n")
            for series_id, details in move_history.items():
                f.write(f"Series ID: {series_id}, Title: {details.get('title', 'Unknown')}, "
                        f"Last Moved To: {details['last_moved_to']}, "
                        f"Timestamp: {details['timestamp']}\n")
    else:
        with open('move_history.txt', 'w', encoding='utf-8') as f:
            f.write("Move History: No move history found.\n")    


def report_free_space_heuristically(series_df, disk_spaces, recommendations_file="recommendations.txt"):
    """Report the free space across drives assuming unlimited moves without making any changes."""
    # Filter disk spaces to include only valid root paths
    disk_space_df = pd.DataFrame(disk_spaces).apply(lambda x: x.str.rstrip('/') if x.dtype == "object" else x)
    disk_space_df = disk_space_df.set_index('path')
    disk_space_df = disk_space_df[disk_space_df.index.isin(valid_root_paths)]

    # Calculate initial free space
    initial_free_space = disk_space_df['freeSpace'] / (1024 ** 3)
    final_free_space = initial_free_space.copy()

    # Sort series by size (smallest first)
    series_df = series_df.sort_values(by='total_size_gb', ascending=True)

    recommendations = []
    
    for index, row in series_df.iterrows():
        best_drive = final_free_space.idxmax()
        if row['root_folder_path'] != best_drive:
            size_gb = row['total_size_gb']
            final_free_space[row['root_folder_path']] += size_gb
            final_free_space[best_drive] -= size_gb
            recommendations.append({
                'series_id': row['series_id'],
                'title': row['title'],
                'current_root': row['root_folder_path'],
                'recommended_root': best_drive,
                'path': row['path'],
                'size_gb': size_gb
            })

    # Write recommendations to a file
    with open(recommendations_file, 'w', encoding='utf-8') as f:
        f.write(f"Number of potential recommendations: {len(recommendations)}\n\n")
        f.write("Recommendations:\n")
        for rec in recommendations:
            f.write(f"Series ID: {rec['series_id']}, Title: {rec['title']}, "
                    f"Current Path: {rec['current_root']}, Recommended Path: {rec['recommended_root']}, "
                    f"Size (GB): {rec['size_gb']:.2f}\n")

    # Ensure the paths are in the same order
    ordered_paths = initial_free_space.index

    # Print current free space as a table
    print("Current free space (in GB) for valid paths:")
    print(initial_free_space.loc[ordered_paths].to_frame(name='Free Space (GB)').to_string(index=True, header=True))

    # Print predicted free space as a table
    print("\nPredicted free space (in GB) after all potential moves:")
    print(final_free_space.loc[ordered_paths].to_frame(name='Free Space (GB)').to_string(index=True, header=True))
    
def balance_free_space_heuristically(series_df, disk_spaces, max_moves=5, dry_run=False):
    """Balance free space across drives using a heuristic approach."""
    disk_space_df = pd.DataFrame(disk_spaces).apply(lambda x: x.str.rstrip('/') if x.dtype == "object" else x)
    disk_space_df = disk_space_df.set_index('path')

    initial_free_space = disk_space_df['freeSpace'] / (1024 ** 3)
    final_free_space = initial_free_space.copy()

    series_df = series_df.sort_values(by='total_size_gb', ascending=True)

    recommendations = []

    for index, row in series_df.iterrows():
        best_drive = final_free_space.idxmax()
        if row['root_folder_path'] != best_drive:
            size_gb = row['total_size_gb']
            final_free_space[row['root_folder_path']] += size_gb
            final_free_space[best_drive] -= size_gb
            recommendations.append({
                'series_id': row['series_id'],
                'title': row['title'],
                'current_root': row['root_folder_path'],
                'recommended_root': best_drive,
                'path': row['path'],
                'size_gb': size_gb
            })

    perform_moves(recommendations, max_moves, dry_run)

def monitor_sonarr_logs(series_id, expected_path, poll_interval=3):
    """Monitor Sonarr logs for successful move completion."""
    log_endpoint = f"{SONARR_API_URL}/log"
    headers = {"X-Api-Key": SONARR_API_KEY}
    
    start_time = datetime.now(timezone.utc)
    
    while (datetime.now(timezone.utc) - start_time).total_seconds() < timeout:
        try:
            response = requests.get(log_endpoint, headers=headers)
            response.raise_for_status()
            logs_data = response.json()

            pattern = re.compile(rf"moved successfully to {re.escape(expected_path)}", re.IGNORECASE)
            for log_entry in logs_data.get('records', []):
                if pattern.search(log_entry.get('message', '')):
                    logger.info(f"Sonarr Log: {log_entry['message']} for Series ID: {series_id}")
                    return True

            time.sleep(poll_interval)
        except requests.exceptions.RequestException as e:
            logger.error(f"Error querying Sonarr logs for Series ID: {series_id}, Expected Path: {expected_path}: {e}")
            time.sleep(poll_interval)
    
    logger.error(f"Timeout reached: Sonarr did not log a successful move for Series ID: {series_id} to {expected_path}.")
    return False


if __name__ == "__main__":
    # Fetch series info
    series_df = get_series_info()
    
    # Fetch disk space info
    disk_spaces = get_free_space_via_api()
    
    # Save move history to file
    save_move_history_to_file()
    
    # Report free space heuristically with unlimited moves (no changes made)
    if not series_df.empty:
        report_free_space_heuristically(series_df, disk_spaces)
    else:
        logger.info("No valid data available for reporting.")
    
    # Balance free space heuristically and perform moves
    if not series_df.empty:
        balance_free_space_heuristically(series_df, disk_spaces, max_moves=max_moves, dry_run=dry_run)
    else:
        logger.info("No valid data available for moving series.")
