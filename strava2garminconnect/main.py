# SPDX-FileCopyrightText: 2024 Steffen Vogel <post@steffenvogel.de>
# SPDX-License-Identifier: Apache-2.0

import os
import logging
import argparse
from datetime import datetime, timedelta
from urllib import request

from strava2garminconnect import garmin, strava, image
from thefuzz import process
from stravaweblib import DataFormat

def get_code(url: str) -> str:
    logging.info("Visit URL and extract code: %s", url)

    return input("OAuth code: ")


def get_mfa() -> str:
    return input("MFA one-time code: ")


def parse_args():
    parser = argparse.ArgumentParser()

    parser.add_argument(
        "--tokens",
        type=str,
        default=os.getenv("TOKENS", os.path.expanduser("~/.strava2garminconnect")),
        help="The path to a directory in which session tokens will be persisted"
    )
    parser.add_argument(
        "--strava-email", type=str, default=os.environ.get("STRAVA_EMAIL"),
        help="The Strava email"
    )
    parser.add_argument(
        "--strava-password",
        type=str,
        default=os.environ.get("STRAVA_PASSWORD"),
        help="The Strava password"
    )
    parser.add_argument(
        "--strava-password-file",
        type=str,
        default=os.environ.get("STRAVA_PASSWORD_FILE"),
        help="The path to a file containing the Strava password"
    )
    parser.add_argument(
        "--strava-client-id", type=str, default=os.environ.get("STRAVA_CLIENT_ID"),
        help="The Strava OAuth client ID"
    )
    parser.add_argument(
        "--strava-client-secret",
        type=str,
        default=os.environ.get("STRAVA_CLIENT_SECRET"),
        help="The Strava OAuth client secret"
    )
    parser.add_argument(
        "--strava-client-secret-file",
        type=str,
        default=os.environ.get("STRAVA_CLIENT_SECRET_FILE"),
        help="The path to a file containging Strava OAuth client secret"
    )

    parser.add_argument(
        "--garmin-email", type=str, default=os.environ.get("GARMIN_EMAIL"),
        help="The Garmin Connect email address"
    )
    parser.add_argument(
        "--garmin-password", type=str, default=os.environ.get("GARMIN_PASSWORD"),
        help="The Garmin Connect password"
    )
    parser.add_argument(
        "--garmin-password-file",
        type=str,
        default=os.environ.get("GARMIN_PASSWORD_FILE"),
        help="The path to a file containing the Garmin Connect password"
    )

    parser.add_argument(
        "--filter-activity-type",
        type=str,
        action="append",
        nargs="*",
        default=os.environ.get("FILTER_ACTIVITY_TYPE"),
        help="Filter the synchronized activities by type"
    )
    parser.add_argument(
        "--filter-last-days",
        type=int,
        default=31,
        help="Import the last N days of activities from Strava"
    )

    parser.add_argument("--sync-name", type=bool, default=True, help="Synchronize activity name")
    parser.add_argument("--sync-photos", type=bool, default=True, help="Synchronize activity photos")
    parser.add_argument("--sync-gear", type=bool, default=True, help="Synchronize gear used in activity")
    parser.add_argument("--sync-gear-threshold", type=int, default=80, help="Similarity threshold for gear matching")

    return parser.parse_args()


def read_secret(secret, secret_file):
    if secret_file is None:
        return secret
    
    with open(secret_file, "r") as file:
        return file.read().strip()


def main():
    logging.basicConfig(level=logging.DEBUG)

    for logger in ["oauthlib", "requests_oauthlib", "stravalib.util.limiter", "urllib3.connectionpool", "PIL", "stravalib.client.BatchedResultsIterator"]:
        logging.getLogger(logger).setLevel(logging.INFO)

    args = parse_args()

    # Read secrets
    garmin_password = read_secret(args.garmin_password, args.garmin_password_file)
    strava_password = read_secret(args.strava_password, args.strava_password_file)
    strava_client_secret = read_secret(args.strava_client_secret, args.strava_client_secret_file)

    sc = strava.Client(
        args.tokens,
        args.strava_email,
        strava_password,
        args.strava_client_id,
        strava_client_secret,
        get_code,
    )
    gc = garmin.Client(args.tokens, args.garmin_email, garmin_password, get_mfa)

    if args.sync_gear:
        profile = gc.get_user_profile()
        garmin_gear = gc.get_gear(profile["id"])
        garmin_gear = {g["uuid"]: g["customMakeModel"] for g in garmin_gear if g["gearStatusName"] == "active"}

    start_date = datetime.now() - timedelta(days=args.filter_last_days)
    end_date = datetime.now()

    for activity in sc.get_activities(after=start_date, before=end_date):
        logging.info("========================================") # just an empty line

        if activity.type.root not in args.filter_activity_type:
            logging.debug("Skipping activity %s", activity.name)
            continue

        logging.info("Processing activity %s", activity.name)

        name, contents = sc.get_activity_data(activity.id, fmt=DataFormat.ORIGINAL)
        content = b"".join(contents)

        try:
            activity_id = gc.upload_activity(name, content)
            logging.info("Activity uploaded to Garmin Connect")
        except garmin.DuplicateActivityError as e:
            logging.warning("Activity has already been uploaded to Garmin Connect with (id=%d)", e.activity_id)
            activity_id = e.activity_id

        if args.sync_name:
            logging.info("Set activity name")
            gc.set_activity_name(activity_id, activity.name)

        if args.sync_gear:
            strava_gear = sc.get_gear(activity.gear_id)
            strava_gear_name = f"{strava_gear.name} {strava_gear.brand_name} {strava_gear.model_name}"

            _, points, garmin_gear_uuid = process.extractOne(strava_gear_name, garmin_gear)
            if points > args.sync_gear_threshold:
                gc.set_activity_gear(activity_id, garmin_gear_uuid)
                logging.info("Matched and updated gear for activity to %s (id=%s)", garmin_gear[garmin_gear_uuid], garmin_gear_uuid)
            else:
                logging.warning("Could not find matching gear for %s (id=%s)", garmin_gear[garmin_gear_uuid], garmin_gear_uuid)

        if args.sync_photos:
            existing_photos = []
            for photo in sc.get_activity_photos(activity.id):
                strava_photo_url = None
                strava_photo_size = 0

                # Find best quality
                for size, url in photo.urls.items():
                    if int(size) > strava_photo_size:
                        strava_photo_url = url
                        strava_photo_size = size

                # Fetch photo
                resp = request.urlopen(strava_photo_url)
                content = resp.read()
            
                try:
                    gc.upload_photo_check_duplicate(activity_id, content, existing_photos)
                    logging.info("Uploaded photo %s", strava_photo_url)
                except garmin.DuplicateActivityPhoto as e:
                    logging.warning(str(e))


if __name__ == "__main__":
    main()
