import requests
import orjson  # Replacing json with orjson for faster JSON handling
import pandas as pd
import logging
import logging.handlers  # For log rotation
import sys
import os
import threading  # For concurrency in monitoring logs
import time
import re
import argparse
import random  # For exponential backoff in retries
from datetime import datetime, timedelta, timezone

# Load configuration from config.json using orjson
with open("config.json", "rb") as config_file:
    config = orjson.loads(config_file.read())

# Assign config values to variables
SONARR_API_URL = config['SONARR_API_URL']
SONARR_API_KEY = config['SONARR_API_KEY']
DEBUG = config['DEBUG']
max_moves = config['max_moves']
dry_run = config['dry_run']
timeout = config['timeout_seconds']
valid_root_paths = [path.rstrip('/').lower() for path in config['valid_root_paths']]
cooldown_days = config['cooldown_days']

# Setup logging with log rotation
logger = logging.getLogger()
logger.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Rotating file handler for logs
file_handler = logging.handlers.RotatingFileHandler(
    'logfile.log', maxBytes=10*1024*1024, backupCount=5, encoding='utf-8'
)
file_handler.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setLevel(logging.DEBUG if DEBUG else logging.INFO)

# Formatter
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler.setFormatter(formatter)
console_handler.setFormatter(formatter)

# Add handlers to logger
logger.addHandler(file_handler)
logger.addHandler(console_handler)

STATE_FILE = 'move_state.json'
MOVE_HISTORY_FILE = 'move_history.json'

class StateManager:
    """Manages state loading and saving."""
    def __init__(self, file_path):
        self.file_path = file_path

    def load_state(self):
        if os.path.exists(self.file_path):
            with open(self.file_path, 'rb') as f:
                return orjson.loads(f.read())
        return {}

    def save_state(self, state):
        with open(self.file_path, 'wb') as f:
            f.write(orjson.dumps(state))

def make_api_request(method, url, headers=None, params=None, json_data=None, max_retries=3):
    """Makes an API request with retries and exponential backoff."""
    for attempt in range(1, max_retries + 1):
        try:
            response = requests.request(method, url, headers=headers, params=params, json=json_data, timeout=10)
            response.raise_for_status()
            return response
        except requests.exceptions.RequestException as e:
            wait_time = 2 ** attempt + random.uniform(0, 1)
            logger.error(
                f"API request error on attempt {attempt} for URL {url}: {e}. Retrying in {wait_time:.2f} seconds."
            )
            time.sleep(wait_time)
    logger.error(f"Failed to make API request to {url} after {max_retries} attempts.")
    raise requests.exceptions.RequestException(f"Failed to make API request to {url} after {max_retries} attempts.")

def get_free_space_via_api():
    """Gets free space for all monitored paths via Sonarr API."""
    disk_space_endpoint = f"{SONARR_API_URL}/diskspace"
    headers = {"X-Api-Key": SONARR_API_KEY}
    try:
        response = make_api_request('GET', disk_space_endpoint, headers=headers)
        return response.json()
    except Exception as e:
        logger.error(f"Error fetching disk space via API: {e}")
        return []

def get_series_info():
    """Gets series information from Sonarr API."""
    series_endpoint = f"{SONARR_API_URL}/series"
    headers = {"X-Api-Key": SONARR_API_KEY}
    series_data = []
    try:
        series_response = make_api_request('GET', series_endpoint, headers=headers)
        series_list = series_response.json()
        for series in series_list:
            series_id = series['id']
            title = series['title']
            path = series['path']
            root_folder_path = series['rootFolderPath'].rstrip('/').lower()
            if root_folder_path in valid_root_paths:
                episode_files_endpoint = f"{SONARR_API_URL}/episodefile"
                params = {"seriesId": series_id}
                episode_files_response = make_api_request('GET', episode_files_endpoint, headers=headers, params=params)
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
    except Exception as e:
        logger.error(f"Error querying Sonarr API: {e}")
        return pd.DataFrame()

def update_move_history(state_manager, series_id, new_root_path, title):
    """Updates move history to avoid ping-ponging."""
    move_history = state_manager.load_state()
    move_history[str(series_id)] = {
        "title": title,
        "last_moved_to": new_root_path,
        "timestamp": datetime.now().isoformat()
    }
    state_manager.save_state(move_history)

def should_move_series(state_manager, series_id, new_root_path):
    """Determines if the series should be moved, considering history."""
    move_history = state_manager.load_state()
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

def move_series(series_id, new_root_path, title, current_path, dry_run=False):
    """Moves a series to a new path in Sonarr."""
    series_endpoint = f"{SONARR_API_URL}/series/{series_id}"
    headers = {
        "X-Api-Key": SONARR_API_KEY,
        "Content-Type": "application/json"
    }
    try:
        logger.info(f"Fetching current series information for Series ID: {series_id}...")
        response = make_api_request('GET', series_endpoint, headers=headers)
        series_info = response.json()
        old_path = series_info['path']
        new_path = f"{new_root_path}/{os.path.basename(old_path)}"
        series_info['path'] = new_path
        move_files = True
        if dry_run:
            logger.info(f"Dry-run: Would move series '{title}' (ID: {series_id}) from {old_path} to {new_path}")
            return True
        logger.info(f"Updating series path from {old_path} to {new_path} and moving files...")
        update_response = make_api_request(
            'PUT', series_endpoint, headers=headers, json_data=series_info, params={"moveFiles": move_files}
        )
        logger.info(f"Series '{title}' (ID: {series_id}) config updated successfully. New path: {new_path}")
        logger.info(f"Sonarr will now move the files; this may take some time depending on the series size.")
        # Trigger rescan
        command_endpoint = f"{SONARR_API_URL}/command"
        rescan_command = {"name": "RescanSeries", "seriesId": series_id}
        make_api_request('POST', command_endpoint, headers=headers, json_data=rescan_command)
        logger.info(f"Triggered rescan for Series ID: {series_id}")
        return True
    except Exception as e:
        logger.error(f"Error moving series '{title}' (ID: {series_id}): {e}")
        return False

def perform_moves(recommendations, max_moves, dry_run=False):
    """Performs the recommended moves and logs total free space before and after all moves."""
    state_manager = StateManager(STATE_FILE)
    move_history_manager = StateManager(MOVE_HISTORY_FILE)
    state = state_manager.load_state()

    # Initialize predicted free space from the API
    disk_spaces = get_free_space_via_api()
    predicted_free_space = {
        disk['path'].rstrip('/').lower(): disk['freeSpace'] / (1024 ** 3)  # Convert bytes to GB
        for disk in disk_spaces
        if disk['path'].rstrip('/').lower() in valid_root_paths
    }

    # Log total free space before any moves
    total_free_space_before = sum(predicted_free_space.values())
    logger.info("Total free space (in GB) BEFORE moves:")
    logger.info(f"Total Free Space: {total_free_space_before:.2f} GB")
    for path, free_space in predicted_free_space.items():
        logger.info(f"{path}: {free_space:.2f} GB")

    moves_completed = 0

    for rec in recommendations:
        if moves_completed >= max_moves:
            break

        series_id = rec['series_id']
        new_root_path = rec['recommended_root']
        current_path = rec['path']
        title = rec['title']
        size_gb = rec['size_gb']

        if should_move_series(move_history_manager, series_id, new_root_path):
            if dry_run:
                logger.info(f"Dry-run: Would move series '{title}' (ID: {series_id}) "
                            f"from {current_path} to {new_root_path}")
                success = True
            else:
                success = move_series(series_id, new_root_path, title, current_path, dry_run)

            if success:
                logger.info(f"Series '{title}' (ID: {series_id}) successfully moved to {new_root_path}.")
                state[str(series_id)] = new_root_path
                state_manager.save_state(state)
                update_move_history(move_history_manager, series_id, new_root_path, title)
                moves_completed += 1

                # Update predicted free space
                old_root_path = rec['current_root']
                predicted_free_space[old_root_path] += size_gb
                predicted_free_space[new_root_path] -= size_gb
            else:
                logger.warning(f"Failed to move Series '{title}' (ID: {series_id}). Not adding to state file.")
        else:
            logger.info(f"Series '{title}' (ID: {series_id}) not eligible for move based on should_move_series check.")

    # Log total free space after all moves
    total_free_space_after = sum(predicted_free_space.values())
    logger.info("Total free space (in GB) AFTER moves:")
    logger.info(f"Total Free Space: {total_free_space_after:.2f} GB")
    for path, free_space in predicted_free_space.items():
        logger.info(f"{path}: {free_space:.2f} GB")

    logger.info(f"Completed {moves_completed} moves out of {max_moves} requested. The script will now exit.")



def balance_free_space_heuristically(series_df, disk_spaces, dry_run=False):
    """Balances free space across drives using a heuristic approach."""
    disk_space_df = pd.DataFrame(disk_spaces)
    disk_space_df['path'] = disk_space_df['path'].str.rstrip('/').str.lower()
    disk_space_df = disk_space_df.set_index('path')

    initial_free_space = disk_space_df['freeSpace'] / (1024 ** 3)  # Convert bytes to GB
    final_free_space = initial_free_space.copy()

    logger.info("Initial free space (in GB) for valid root paths:")
    for path in valid_root_paths:
        if path in initial_free_space:
            logger.info(f"{path}: {initial_free_space[path]:.2f} GB")

    series_df = series_df.copy()
    series_df['available_space'] = series_df['root_folder_path'].map(final_free_space)
    series_df = series_df.sort_values(by=['available_space', 'total_size_gb'], ascending=[True, False])

    state_manager = StateManager(STATE_FILE)
    state = state_manager.load_state()

    recommendations = []
    total_size_to_move_gb = 0
    moves_count = 0

    for _, row in series_df.iterrows():
        if moves_count >= max_moves:
            break

        series_id = row['series_id']
        if str(series_id) in state:
            logger.debug(f"Skipping recommendation for Series ID: {series_id} as it is already in the state file.")
            continue

        suitable_drives = final_free_space[final_free_space >= row['total_size_gb']]
        if suitable_drives.empty:
            logger.warning(f"No suitable drives found for Series ID: {series_id}, Title: {row['title']}")
            continue

        best_drive = suitable_drives.idxmax()
        if row['root_folder_path'] != best_drive:
            size_gb = row['total_size_gb']
            final_free_space[row['root_folder_path']] += size_gb
            final_free_space[best_drive] -= size_gb
            recommendations.append({
                'series_id': series_id,
                'title': row['title'],
                'current_root': row['root_folder_path'],
                'recommended_root': best_drive,
                'path': row['path'],
                'size_gb': size_gb
            })
            total_size_to_move_gb += size_gb
            moves_count += 1

    logger.info("Free space (in GB) after all potential moves for valid root paths:")
    for path in valid_root_paths:
        if path in final_free_space:
            logger.info(f"{path}: {final_free_space[path]:.2f} GB")

    logger.info(f"Total size to be moved: {total_size_to_move_gb:.2f} GB across {moves_count} series")
    perform_moves(recommendations, max_moves, dry_run)


def monitor_sonarr_logs(series_id, expected_path, poll_interval=3):
    """Monitors Sonarr logs for successful move completion asynchronously."""
    if dry_run:
        logger.info(f"Dry-run: Skipping Sonarr log monitoring for Series ID: {series_id} to {expected_path}.")
        return True
    log_endpoint = f"{SONARR_API_URL}/log"
    headers = {"X-Api-Key": SONARR_API_KEY}
    start_time = datetime.now(timezone.utc)
    while (datetime.now(timezone.utc) - start_time).total_seconds() < timeout:
        try:
            response = make_api_request('GET', log_endpoint, headers=headers)
            logs_data = response.json()
            pattern = re.compile(rf"moved successfully to {re.escape(expected_path)}", re.IGNORECASE)
            for log_entry in logs_data.get('records', []):
                if pattern.search(log_entry.get('message', '')):
                    logger.info(f"Sonarr Log: {log_entry['message']} for Series ID: {series_id}")
                    return True
            time.sleep(poll_interval)
        except Exception as e:
            logger.error(f"Error querying Sonarr logs for Series ID: {series_id}, Expected Path: {expected_path}: {e}")
            time.sleep(poll_interval)
    logger.error(f"Timeout reached: Sonarr did not log a successful move for Series ID: {series_id} to {expected_path}.")
    return False

def validate_config():
    """Validates critical configuration parameters."""
    if not SONARR_API_URL or not SONARR_API_KEY:
        logger.error("Critical configuration missing: SONARR_API_URL and SONARR_API_KEY must be set.")
        sys.exit(1)

def print_move_history(state_manager):
    """Prints the move history in a human-readable format."""
    try:
        move_history = state_manager.load_state()
        if not move_history:
            logger.info("No move history found.")
            return
        logger.info("Move History:")
        logger.info("=" * 40)
        for series_id, data in move_history.items():
            last_moved_to = data.get('last_moved_to', 'Unknown')
            title = data.get('title', 'Unknown')
            timestamp = data.get('timestamp', 'Unknown')
            logger.info(f"Series ID: {series_id}, Title: {title}, Last Moved To: {last_moved_to}, Timestamp: {timestamp}")
    except Exception as e:
        logger.error(f"An unexpected error occurred while printing move history: {e}")

if __name__ == "__main__":
    # Argument parsing
    parser = argparse.ArgumentParser(description="Sonarr Series Management Script")
    parser.add_argument("--dry-run", action="store_true", help="Run the script in dry-run mode")
    parser.add_argument("--max-moves", type=int, help="Override the maximum number of moves")
    args = parser.parse_args()

    # Override config values if provided via command line
    if args.dry_run:
        dry_run = True
    if args.max_moves:
        max_moves = args.max_moves

    # Validate configuration before proceeding
    validate_config()

    # Initialize State Managers
    state_manager = StateManager(STATE_FILE)
    move_history_manager = StateManager(MOVE_HISTORY_FILE)

    # Print move history
    logger.info("Printing a list of historic moves.")
    print_move_history(move_history_manager)

    # Fetch series info
    series_df = get_series_info()

    # Fetch disk space info
    disk_spaces = get_free_space_via_api()

    # Balance free space heuristically and perform moves
    if not series_df.empty and disk_spaces:
        balance_free_space_heuristically(series_df, disk_spaces, dry_run=dry_run)
    else:
        logger.info("No valid data available for moving series.")
