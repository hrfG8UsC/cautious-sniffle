from datetime import datetime, timedelta, timezone
import json
import math
import os
from pathlib import Path
import re
import shutil
import tempfile
import time

from lxml import etree
from mega import Mega
import requests


mebibyte = 1024 ** 2
CHUNK_SIZE = 300 * mebibyte # size for chunk-downloading of images and videos


def main():
    username = "skinnyboyonweb"
    username = "T4stytwink"
    with requests.Session() as s:
        tweets_datas = _do(s, username)
    _upload_tweets_to_mega(tweets_datas, username)


class TemporaryLocalDownloadDir():
    """Prepare temporary download directory.

    The files will be downloaded to this local directory, in order to upload
    them to MEGA from there.
    """

    def __init__(self):
        child = f'tw_{time.strftime("%Y-%m-%dT%H-%M-%SZ", time.gmtime())}'
        # e.g. /tmp/tw_...
        self.dirname = (Path(tempfile.gettempdir()) / child).resolve()

    def __enter__(self):
        try:
            os.mkdir(self.dirname)
        except FileExistsError:
            # directory already exists for whatever reason, no problem
            pass
        return self.dirname

    def __exit__(self, exc_type, exc_value, exc_traceback):
        shutil.rmtree(self.dirname)


def _do(session: requests.Session, username: str):
    tweets_datas = _get_cached_if_not_missed(username, timedelta(days=1))
    if not tweets_datas:
        tweets_datas = []
        medias = f"https://nitter.it/{username}/media"
        tweet_ids = []
        pagecount = 0
        pagelink = medias
        while True:
            pagecount += 1
            print("request no.", pagecount, pagelink)
            response = session.get(pagelink)
            response.raise_for_status()
            root: etree._Element = etree.fromstring(response.text)

            tweets_on_this_page: list[etree._Element] = [
                e for e in root.find(".//div[@class='timeline']")
                if "timeline-item" in e.get("class")
            ]
            for tweet in tweets_on_this_page:
                tweetlink = tweet[0].get("href")
                match = re.search(r"/status/(\d+)(?:#.*)?$", tweetlink)
                if match:
                    tweet_ids.append(match.group(1))

            showmore_link: etree._Element = root.find(".//div[@class='show-more']/a[@href]")
            if showmore_link is None:
                break
            cursor = showmore_link.get("href")
            pagelink = medias + cursor

        print()
        print(tweet_ids)

        base_url = "https://cdn.syndication.twimg.com/tweet?id="
        for tweet_id in tweet_ids:
            response = session.get(base_url + tweet_id)
            tweets_datas.append(response.json())

        with open('.cache-' + username, 'w+', encoding='utf-8') as f:
            cachejson = {
                'ts': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
                'data': tweets_datas
            }
            json.dump(cachejson, f, indent=2)

    return tweets_datas


def _upload_tweets_to_mega(tweets_datas, username):
    mega = Mega()
    mega.login(*_load_mega_creds())


    for tweet_data in tweets_datas:
        photo_urls = [p.get('url') for p in tweet_data.get('photos', [])]
        video_thumbnail_url = tweet_data.get('video', {}).get('poster')
        video_url = None
        video_variants = tweet_data.get('video', {}).get('variants', [])
        for variant in video_variants:
            video_url = variant.get('src')
            if video_url is not None and variant.get('type') == 'video/mp4':
                break

        print()
        print('+' * 20)
        print(f'https://nitter.it/i/status/{tweet_data.get("id_str", "")}')
        print('Photos:', 'None' if not photo_urls else '')
        _ = [print('* ' + p) for p in photo_urls]
        print('Video:', video_url)
        print('Video thumbnail:', video_thumbnail_url)
        print('+' * 20)

        downloaded_files = []
        with TemporaryLocalDownloadDir() as dirname:
            t = str(round(time.time()))
            json_target = dirname / f'tw_info_{t}.json'
            with open(json_target, 'w+', encoding='utf-8') as f:
                json.dump(tweet_data, f)
            downloaded_files.append(json_target)
            photo_targets = None
            video_target = None
            if photo_urls:
                photo_targets = [dirname / f'tw_photo_{t}_{i}.jpg' for i in range(len(photo_urls))]
                for j, url in enumerate(photo_urls):
                    _download_something_to_local_fs(url, photo_targets[j])
                    downloaded_files.append(photo_targets[j])
            if video_url:
                video_target = dirname / f'tw_video_{t}.mp4'
                _download_something_to_local_fs(video_url, video_target)
                downloaded_files.append(video_target)
                thumb_target = dirname / f'tw_thumb_{t}.jpg'
                _download_something_to_local_fs(video_thumbnail_url, thumb_target)
                downloaded_files.append(thumb_target)

            print(f'Downloaded files: {downloaded_files}')
            for filename in downloaded_files:
                _upload_to_mega(mega, filename, 'tw/' + username)



    mega.logout_session()


def _get_cached_if_not_missed(username: str, period: timedelta):
    try:
        with open('.cache-' + username, encoding='utf-8') as f:
            data = json.load(f)
    except FileNotFoundError:
        return
    cache_timestamp_str = data.get('ts')
    if cache_timestamp_str is None:
        return
    cache_timestamp = datetime.strptime(cache_timestamp_str, '%Y-%m-%dT%H:%M:%S%z')
    cache_age = cache_timestamp - datetime.now(tz=timezone.utc)
    print(f'{cache_age=}')
    if cache_age < period:
        return data.get('data')


def _upload_to_mega(mega: Mega, filename: str, target_folder_on_mega: str):
    print(f'Uploading {filename} to MEGA...')
    if mega.find(filename, exclude_deleted=True) is None:
        mega.upload(filename, mega.find(target_folder_on_mega)[0])
        print('Upload finished.')
    else:
        print('Already exists. Skipped.')


def _download_something_to_local_fs(url: str, target_file):
    """Download the content at `url` to `target_file` in the local filesystem."""
    r = requests.get(url, stream=True)
    try:
        r.raise_for_status()
    except requests.HTTPError as e:
        print(e)
        print(f'Skipped downloading "{target_file}".')
    else:
        with open(target_file, 'wb') as f:
            for chunk in r.iter_content(chunk_size=CHUNK_SIZE):
                f.write(chunk)
            print(f'Downloaded "{target_file}".')


def _load_mega_creds():
    username = os.getenv('MEGA_EMAIL')
    password = os.getenv('MEGA_PASSWORD')
    if None in (username, password):
        try:
            with open('creds_mega.json') as f:
                username, password = tuple(json.load(f))
        except (FileNotFoundError, json.JSONDecodeError):
            pass
    return username, password


if __name__ == "__main__":
    main()
