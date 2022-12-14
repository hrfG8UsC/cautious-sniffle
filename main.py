from collections import namedtuple
from datetime import datetime, timezone
import json
import html
from html.parser import HTMLParser
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
import traceback
from typing import Generator, Iterable
from urllib.parse import urlparse

from lxml import etree
from lxml.cssselect import CSSSelector
from mega import Mega
import requests


mebibyte = 1024 ** 2
CHUNK_SIZE = 300 * mebibyte # size for chunk-downloading of images and videos
FFMPEG_BIN = "ffmpeg"

one_page_only = False  # for debug

IS_GH_ACTION = os.getenv("GITHUB_ACTION") is not None

USER_AGENT = "Mozilla/5.0 (Windows NT 6.3; Win64; x64; rv:103.0) Gecko/20100101 Firefox/103.0"

# format is e.g.: Nov 1, 2022 · 4:34 PM UTC
TWEET_DATE_PATTERN = re.compile(
    r'^(?P<month>\w{3}) (?P<day>\d\d?), (?P<year>\d{4}) · '
    r'(?P<hour>[01]?\d):(?P<minute>\d\d) (?P<ampm>AM|PM) UTC$'
)
MONTHS = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]


class TemporaryLocalDownloadDir():
    """Prepare temporary download directory.

    The files will be downloaded to this local directory, in order to upload
    them to MEGA from there.

    >>> with TemporaryLocalDownloadDir() as tmpdir:
    ...     print(tmpdir)
    /tmp/tw_2022-11-25T17-45-12Z
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


class NitterInstanceSwitcher():
    """Manages switching between Nitter instances.

    >>> instance = NitterInstanceSwitcher.new()
    >>> print(instance)
    https://nitter.net
    """

    # https://farside.link/
    # https://twiiit.com/
    # https://xnaas.github.io/nitter-instances/
    # https://github.com/zedeus/nitter/wiki/Instances

    sleepseconds = 3
    switches = 0
    current_instance = ''

    bad_instances = {
        # instances that use Cloudflare are bad because it messes with the
        # .m3u8 video playlist files randomly; for list see
        # https://github.com/zedeus/nitter/wiki/Instances#public
        "nitter.esmailelbob.xyz",  # old video format
        "nitter.domain.glass",  # cloudflare
        "nitter.winscloud.net",  # cloudflare
        "twtr.bch.bar",  # cloudflare
        "twitter.dr460nf1r3.org",  # cloudflare
        "nitter.garudalinux.org",  # cloudflare
        "nitter.rawbit.ninja",  # cloudflare
        "nitter.privacytools.io",  # cloudflare
        "nitter.sneed.network",  # cloudflare, no medias
        "n.sneed.network",  # cloudflare, no medias
        "nitter.d420.de",  # cloudflare
    }

    request_errors = (requests.exceptions.ConnectionError, requests.exceptions.ReadTimeout)

    @classmethod
    def add_bad_instance(cls, instance_url: str):
        cls.bad_instances.add(instance_url.removeprefix("https://"))

    @classmethod
    def new(cls, session: requests.Session) -> str:
        if cls.switches > 0:  # no sleep when doing the first switch ever
            time.sleep(cls.sleepseconds)
        try:
            # twiiit.com/twitter redirects to a random nitter instance
            response = session.get("https://twiiit.com/twitter")
        except cls.request_errors as exc:
            print(f"Nitter instance switch unsuccessful: {exc}")
            return cls.new(session)  # try switch again
        if not response.ok:
            return cls.new(session)  # try switch again
        hostname = urlparse(response.url).hostname
        if not hostname or hostname in cls.bad_instances:
            print(f'Nitter instance switch unsuccessful: bad instance "{hostname}"')
            return cls.new(session)  # try switch again
        cls.switches += 1
        previous_instance = cls.current_instance or '(None)'
        cls.current_instance = "https://" + hostname
        print(
            f"Nitter instance switch #{cls.switches}: "
            f"{previous_instance} --> {cls.current_instance}"
        )
        return cls.current_instance


class HtmlStripper(HTMLParser):
    """Remove all HTML markup and only keep the text content."""
    stripped_text = ''
    def handle_data(self, data):
        self.stripped_text += data


TweetElementWithInstance = namedtuple("TweetElementWithInstance", [
    "instance_url",  # str
    "element"  # etree._Element
])


TweetData = namedtuple("TweetData", [
    "link",
    "author_fullname",
    "timestamp",
    "text_html",
    "text_plain",
    "photo_urls",
    "gif_urls",
    "video_url",
    "videothumb_url"
])


def main(username: str, tempdir: Path):
    megalogger = logging.getLogger("mega")
    megalogger.setLevel(logging.DEBUG)
    print_to_console = logging.StreamHandler()
    print_to_console.setLevel(logging.DEBUG)
    megalogger.addHandler(print_to_console)

    with requests.Session() as session:
        session.headers.update({ "User-Agent": USER_AGENT })
        for tweet_element in _fetch_tweet_elements(session, username):
            try:
                tweet_data = _parse_tweet_element(tweet_element)
                downloaded_file_paths = _download_tweet_data(tweet_data, tempdir)
                _upload_files_to_mega(downloaded_file_paths, 'tw/' + username)
            except Exception:
                traceback.print_exc()


def _download_tweet_data(tweet_data: TweetData, directory: Path):
    print()
    print('+' * 20)
    print(tweet_data.link)
    print(f'By: {tweet_data.author_fullname}')
    print(f'At: {tweet_data.timestamp}')
    print(f'HTML: {tweet_data.text_html}')
    print(f'Stripped: {tweet_data.text_plain}')
    print('Photos:', 'None' if not tweet_data.photo_urls else '')
    for p in tweet_data.photo_urls:
        print(f'* {p}')
    print('GIFs:', 'None' if not tweet_data.gif_urls else '')
    for p in tweet_data.gif_urls:
        print(f'* {p}')
    print('Video:', tweet_data.video_url)
    print('Video thumbnail:', tweet_data.videothumb_url)
    print('+' * 20)

    t = str(time.time())
    downloaded_file_names: list[Path] = []

    json_target = directory / f'tw_info_{t}.json'
    json_data = tweet_data._asdict()
    json_data["downloaded_at"] = datetime.now(tz=timezone.utc).isoformat()
    with open(json_target, 'w+', encoding='utf-8') as f:
        json.dump(json_data, f, ensure_ascii=False, indent=2)
    downloaded_file_names.append(json_target)

    for i, photo_url in enumerate(tweet_data.photo_urls):
        photo_target = directory / f'tw_photo_{t}_{i}.jpg'
        _download_something_to_local_fs(photo_url, photo_target)
        downloaded_file_names.append(photo_target)

    for i, gif_url in enumerate(tweet_data.gif_urls):
        gif_target = directory / f'tw_gif_{t}_{i}.mp4'
        _download_something_to_local_fs(gif_url, gif_target)
        downloaded_file_names.append(gif_target)

    if tweet_data.video_url:
        video_target = directory / f'tw_video_{t}.mp4'
        cmd = [FFMPEG_BIN, "-i", tweet_data.video_url, "-c", "copy", str(video_target)]
        subprocess.call(cmd)
        downloaded_file_names.append(video_target)
        thumb_target = directory / f'tw_thumb_{t}.jpg'
        _download_something_to_local_fs(tweet_data.videothumb_url, thumb_target)
        downloaded_file_names.append(thumb_target)

    return downloaded_file_names


def _fetch_tweet_elements(session: requests.Session, username: str) -> Generator[TweetElementWithInstance, None, None]:
    tweet_selector = CSSSelector("div.timeline div.timeline-item:not(.show-more)")
    pagecount = 0
    instance_url = ''
    pages_until_instanceswitch = 0
    cursor = ''
    while True and (pagecount < 1 if one_page_only else True):
        pagecount += 1
        pages_until_instanceswitch -= 1

        if pages_until_instanceswitch <= 0:
            pages_until_instanceswitch = 3
            instance_url = _get_random_nitter_instance_url(session)
        pagelink = f"{instance_url}/{username}/media{cursor}"
        print(f"request no. {pagecount} {pagelink}")
        response = session.get(pagelink)
        response.raise_for_status()

        try:
            # the default parser raises an error when encountering unquoted attribute values
            root: etree._Element = etree.fromstring(response.text, parser=etree.HTMLParser())
        except etree.XMLSyntaxError:
            NitterInstanceSwitcher.add_bad_instance(instance_url)
            print("XML syntax error, have to switch instance")
            pages_until_instanceswitch = 0
            pagecount -= 1
            continue

        # if there is a video on this page and HLS is disabled, then switch instance
        enable_hls_link = _safe_select('div.video-overlay > form[action="/enablehls"]', root)
        if enable_hls_link is not None:
            NitterInstanceSwitcher.add_bad_instance(instance_url)
            print("HLS disabled, have to switch instance")
            pages_until_instanceswitch = 0
            pagecount -= 1
            continue

        for tweet_element in tweet_selector(root):
            yield TweetElementWithInstance(instance_url, tweet_element)

        showmore_link: etree._Element = _safe_select("div.timeline-item + div.show-more > a[href]", root)
        if showmore_link is None:
            break
        cursor = showmore_link.get("href")


def _get_random_nitter_instance_url(session: requests.Session) -> str:
    return NitterInstanceSwitcher.new(session)


def _safe_select(css_selector: str, element) -> "etree._Element|None":
    """Get the first element matching the selector, and `None` if nothing matches."""
    sel = CSSSelector(css_selector)
    list_of_elements = sel(element)
    if len(list_of_elements) > 0:
        return list_of_elements[0]


def _parse_tweet_element(tweet_element: TweetElementWithInstance) -> TweetData:
    return TweetData(
        _parse_tweet_link(tweet_element),
        _parse_tweet_author(tweet_element),
        _parse_tweet_date(tweet_element).isoformat(),
        *_parse_tweet_text(tweet_element),
        list(_parse_tweet_photos(tweet_element)),
        list(_parse_tweet_gifs(tweet_element)),
        *_parse_tweet_video(tweet_element)
    )


def _parse_tweet_author(tweet_element: TweetElementWithInstance) -> str:
    fullname_link = _safe_select("a.fullname", tweet_element.element)
    if fullname_link is None:
        return ''
    return fullname_link.text


def _parse_tweet_date(tweet_element: TweetElementWithInstance) -> datetime:
    fallback_date = datetime.fromtimestamp(0, tz=timezone.utc)
    date_link = _safe_select(".tweet-date > a:first-child", tweet_element.element)
    if date_link is None:
        return fallback_date
    tweet_date = html.unescape(date_link.get("title"))
    match = TWEET_DATE_PATTERN.match(tweet_date)
    if not match:
        return fallback_date
    hour = int(match.group("hour"))
    hour += 12 if match.group("ampm") == "PM" and hour != 12 else 0
    month = match.group("month").lower()
    if month not in MONTHS:
        return fallback_date
    return datetime(
        month = MONTHS.index(month) + 1,
        day = int(match.group("day")),
        year = int(match.group("year")),
        hour = hour,
        minute = int(match.group("minute")),
        tzinfo = timezone.utc
    )


def _parse_tweet_link(tweet_element: TweetElementWithInstance) -> str:
    tweet_link = _safe_select(".tweet-link", tweet_element.element)
    if tweet_link is None:
        return ''
    return tweet_element.instance_url + tweet_link.get("href")


def _parse_tweet_text(tweet_element: TweetElementWithInstance) -> tuple[str, str]:
    content_div = _safe_select("div.tweet-content.media-body", tweet_element.element)
    if content_div is None:
        return '', ''
    tweet_html = etree.tostring(content_div).decode().strip()
    parser = HtmlStripper()
    parser.feed(tweet_html)
    tweet_plaintext = parser.stripped_text.strip()
    return tweet_html, tweet_plaintext


def _parse_tweet_photos(tweet_element: TweetElementWithInstance) -> Generator[str, None, None]:
    attachments_div = _safe_select("div.attachments", tweet_element.element)
    if attachments_div is None:
        return
    for image_link_element in CSSSelector("a.still-image")(attachments_div):
        yield tweet_element.instance_url + image_link_element.get("href")


def _parse_tweet_gifs(tweet_element: TweetElementWithInstance) -> Generator[str, None, None]:
    attachments_div = _safe_select("div.attachments", tweet_element.element)
    if attachments_div is None:
        return
    for gif_element in CSSSelector("video.gif source")(attachments_div):
        # no clue why, but this following "if" check is necessary for whatever reason
        if gif_element is not None and gif_element.get("src") is not None:
            yield tweet_element.instance_url + gif_element.get("src")


def _parse_tweet_video(tweet_element: TweetElementWithInstance) -> tuple[str, str]:
    # this instance apparently uses a different format:
    # https://nitter.esmailelbob.xyz/hypersupermia/status/1587660697235853313#m
    attachments_div = _safe_select("div.attachments", tweet_element.element)
    if attachments_div is None:
        return '', ''
    video_element = _safe_select(".video-container > video", attachments_div)
    if video_element is None:
        return '', ''
    return (
        tweet_element.instance_url + video_element.get("data-url"),
        tweet_element.instance_url + video_element.get("poster")
    )


def _upload_files_to_mega(filepaths: Iterable[Path], target_folder_name: str):
    if len(filepaths) == 0:
        return

    mega = Mega()
    mega.login(*_load_mega_creds())

    target_folder = mega.find(target_folder_name, exclude_deleted=True)
    if target_folder is None:
        # folder does not exist, so create it
        node_ids = mega.create_folder(target_folder_name)
        print(f'Created new folder: {target_folder_name}')
        target_folder = node_ids[target_folder_name.rsplit('/', maxsplit=1)[-1]]
    else:
        # folder exists
        target_folder = target_folder[0]

    for filepath in filepaths:
        target_filename = filepath.name
        mega.upload(filepath, dest=target_folder, dest_filename=target_filename)
        print(f'Uploaded {target_filename} to MEGA.')

    mega.logout_session()


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
    if IS_GH_ACTION:
        username = sys.argv[1]
        tempdir = Path("dl")
        tempdir.mkdir()
        main(username, tempdir)
    else:
        username = "skinnyboyonweb"
        username = "T4stytwink"
        username = "NetflixNordic"
        username = "conorsworld2003"
        username = "JadenHeart3"
        username = "tillwehaveface3"
        with TemporaryLocalDownloadDir() as tempdir:
            main(username, tempdir)
