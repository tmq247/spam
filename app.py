import json
from flask import Flask, Response
from config import TELEGRAM_BOT_TOKEN, TZ_OFFSET
from datetime import datetime, timedelta, timezone
import logging
from sheets import SheetsService, edit_entry_multiple_fields, parse_time_mins
import requests
from helper import calc_next_run
import gc

app = Flask(__name__)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)

logger = logging.getLogger(__name__)


@app.route("/", methods=["GET", "POST"])
def run():
    # TODO - allow only POST
    # TODO - add authentication
    sheets_service = SheetsService()
    now = datetime.now(timezone(timedelta(hours=TZ_OFFSET)))
    parsed_time = parse_time_mins(now)
    entries = sheets_service.get_entries_by_nextrun(parsed_time)

    if len(entries) < 1:
        logger.info("No messages sent")
        gc.collect()

        return Response(status=200)

    for i, row in entries:
        chat_id = row["chat_id"]
        content = row["content"]
        photo_id = row["photo_id"]
        photo_group_id = str(row["photo_group_id"])
        crontab = row["crontab"]
        previous_message_id = str(row["previous_message_id"])

        bot_message_id, err = send_message(chat_id, content, photo_id, photo_group_id)
        if row["option_delete_previous"] != "" and previous_message_id != "":
            delete_message(chat_id, previous_message_id)

        # calculate and update next run time
        user_tz_offset = sheets_service.retrieve_tz(chat_id)
        user_nextrun_ts, db_nextrun_ts = calc_next_run(crontab, user_tz_offset)

        updated_entry = edit_entry_multiple_fields(
            row.to_frame().T,
            {
                "nextrun_ts": db_nextrun_ts,
                "user_nextrun_ts": user_nextrun_ts,
                "previous_message_id": str(bot_message_id),
                "remarks": "" if err is None else err,
                "removed_ts": "" if err is None else parsed_time,
            },
        )
        sheets_service.update_entry(updated_entry)

    gc.collect()  # https://github.com/googleapis/google-api-python-client/issues/535

    return Response(status=200)


def prepare_photos(photo_id, content):
    photo_ids = photo_id.split(";")
    media = []
    files = {}
    for i, photo_id in enumerate(photo_ids):
        file_details_endpoint = (
            "https://api.telegram.org/bot{}/getFile?file_id={}".format(
                TELEGRAM_BOT_TOKEN, photo_id
            )
        )
        file_details_response = requests.get(file_details_endpoint)
        file_url = "https://api.telegram.org/file/bot{}/{}".format(
            TELEGRAM_BOT_TOKEN, file_details_response.json()["result"]["file_path"]
        )
        file_response = requests.get(file_url)
        open("photo-%d" % i, "wb").write(file_response.content)
        files["photo-%d" % i] = open("photo-%d" % i, "rb")

        media.append(
            {
                "type": "photo",
                "media": "attach://photo-%d" % i,
                "caption": content if i <= 0 else "",
            }
        )
    return json.dumps(media), files


def send_message(chat_id, content, photo_id, photo_group_id):
    if photo_group_id != "":  # media group
        media, files = prepare_photos(photo_id, content)
        telebot_api_endpoint = (
            "https://api.telegram.org/bot{}/sendMediaGroup?chat_id={}&media={}".format(
                TELEGRAM_BOT_TOKEN, chat_id, media
            )
        )
        response = requests.post(telebot_api_endpoint, files=files)
    elif photo_id != "":  # single photo
        telebot_api_endpoint = "https://api.telegram.org/bot{}/sendPhoto?chat_id={}&photo={}&caption={}".format(
            TELEGRAM_BOT_TOKEN, chat_id, photo_id, content
        )
        response = requests.get(telebot_api_endpoint)
    else:  # normal message
        telebot_api_endpoint = (
            "https://api.telegram.org/bot{}/sendMessage?chat_id={}&text={}".format(
                TELEGRAM_BOT_TOKEN, chat_id, content
            )
        )
        response = requests.get(telebot_api_endpoint)

    logger.info(
        'Message sent, chat_id=%s, message="%s", response_status=%s',
        chat_id,
        content,
        response.status_code,
    )
    if response.status_code != 200:
        return "", "Error {}: {}".format(
            response.status_code, response.json()["description"]
        )

    if photo_group_id != "":
        return (
            ";".join(
                [str(message["message_id"]) for message in response.json()["result"]]
            ),
            None,
        )

    return response.json()["result"]["message_id"], None


def delete_message(chat_id, previous_message_id):
    for message_id in previous_message_id.split(";"):
        telebot_api_endpoint = "https://api.telegram.org/bot{}/deleteMessage?chat_id={}&message_id={}".format(
            TELEGRAM_BOT_TOKEN, chat_id, message_id
        )
        response = requests.get(telebot_api_endpoint)
        logger.info(
            'Message deleted, chat_id=%s, message_id="%s", response_status=%s',
            chat_id,
            message_id,
            response.status_code,
        )
    return response.json()["ok"]


if __name__ == "__main__":
    app.run(debug=True)
