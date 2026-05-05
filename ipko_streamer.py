#!/usr/bin/env python3
"""
IPKO TV StreamUpdater Script
- Login via Playwright (browser required for cookie chain)
- Select profile
- Get all channels from ZapList
- Get HLS stream URLs via WebLiveStream API
- Push streams to XAccel
- Monitor and refresh tokens before expiry (~24h)
"""
import json, time, sys, os, traceback
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
import requests as req
from playwright.sync_api import sync_playwright

# Config
IPKO_USERNAME = "isma@hotmail.nl"
IPKO_PASSWORD = "Delija1969."
XACCEL_URL = "http://62.210.93.11"
XACCEL_TOKEN = "Bcxq46W1VO1qknQIJGMNxU7k"
STARGATE_URL = "https://stargate.ipko.tv/api"
STREAM_PREFIX = "IPKO | "
XACCEL_PROFILE_ID = 1
MAX_WORKERS = 3
TOKEN_REFRESH_HOURS = 20
WORK_DIR = os.path.dirname(os.path.abspath(__file__))


def now():
    return datetime.now().strftime('%Y-%m-%d %H:%M:%S')


class IPKOSession:

    def __init__(self):
        self.session_token = None
        self.cookies = {}
        self.token_expires = 0

    def login(self):
        print(f"[{now()}] Logging in to IPKO TV...")
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                viewport={"width": 1280, "height": 720},
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            )
            page = context.new_page()

            page.goto("https://ipko.tv/login", wait_until="networkidle", timeout=30000)
            time.sleep(2)

            try:
                page.click('text=PRANOJ', timeout=2000)
                time.sleep(0.5)
            except:
                pass

            page.fill('input[name="username"]', IPKO_USERNAME)
            page.fill('input[type="password"]', IPKO_PASSWORD)

            with page.expect_response(lambda r: 'ExecLoginUnPass' in r.url, timeout=20000) as resp_info:
                try:
                    page.click('span.b_button_component:has-text("KYÇU")', timeout=3000)
                except:
                    page.press('input[type="password"]', 'Enter')

            login_resp = resp_info.value
            lr = login_resp.json()

            if lr.get('login_result') != 'lr_success':
                print(f"[{now()}] Login failed: {lr.get('login_result')}")
                browser.close()
                return False

            self.session_token = lr['session_data']['session_token']
            print(f"[{now()}] Login success! Token: {self.session_token[:20]}...")

            time.sleep(4)
            try:
                page.wait_for_url("**/setting-required-data**", timeout=10000)
            except:
                pass

            self.cookies = {c['name']: c['value'] for c in context.cookies()}

            try:
                avatar = page.query_selector('span.avatar_wrap')
                if avatar:
                    avatar.click()
                    time.sleep(3)
                    page.wait_for_load_state("networkidle", timeout=10000)
                    print(f"[{now()}] Profile selected")
            except Exception as e:
                print(f"[{now()}] Profile select warning: {e}")

            browser.close()

        self.token_expires = time.time() + TOKEN_REFRESH_HOURS * 3600
        return True

    def api_call(self, endpoint, body=None):
        url = f"{STARGATE_URL}/{endpoint}"
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f'APIGW-AUTH-TOK {self.session_token}',
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
            'Origin': 'https://ipko.tv',
            'Referer': 'https://ipko.tv/',
        }
        s = req.Session()
        for name, value in self.cookies.items():
            s.cookies.set(name, value, domain='.ipko.tv')
            s.cookies.set(name, value, domain='stargate.ipko.tv')

        resp = s.post(url, json=body or {}, headers=headers, timeout=30)
        resp.raise_for_status()
        return resp.json()

    def get_channels(self):
        data = self.api_call('titan.tv.WebEpg/ZapList', {'includeRadioStations': False})
        channels = []
        for item in data.get('data', []):
            ch = item.get('channel', {})
            if ch.get('id', '').startswith('linear://'):
                continue
            channels.append({
                'id': ch.get('id', ''),
                'title': ch.get('title', ''),
                'number': ch.get('number', ''),
                'friendly_name': ch.get('friendly_name', ''),
                'logo': ch.get('logo', ''),
                'group': ch.get('group', ''),
            })
        return channels

    def get_stream_url(self, channel_id):
        data = self.api_call('titan.tv.ContentService/WebLiveStream', {'channel_id': channel_id})
        if not data.get('success'):
            return None
        streams = data.get('streams', [])
        for s in streams:
            if s.get('proto') == 'hls' and not s.get('drm_enabled'):
                return s.get('stream_url')
        if streams:
            return streams[0].get('stream_url')
        return None

    def is_token_valid(self):
        return time.time() < self.token_expires

    def refresh_if_needed(self):
        if not self.is_token_valid():
            print(f"[{now()}] Token expired, re-logging in...")
            return self.login()
        return True


class XAccelClient:

    def __init__(self):
        self.base_url = XACCEL_URL
        self.token = XACCEL_TOKEN

    def _headers(self):
        return {'Authorization': f'token {self.token}'}

    def _headers_action(self):
        return {'Authorization': f'Token {self.token}'}

    def fetch_stream_stats(self):
        resp = req.get(f"{self.base_url}/api/stream/stats", headers=self._headers(), timeout=30)
        resp.raise_for_status()
        return resp.json()

    def create_stream(self, name, url):
        json_data = {
            'name': name,
            'input_urls': [url],
            'profile_id': XACCEL_PROFILE_ID,
            'rate_emulation': 'no',
            'video_encoders': [{'codec': 'copy'}],
            'audio_encoders': [{'codec': 'copy'}],
            'outputs': [
                {'protocol': 'hls', 'filename': 'index.m3u8'},
                {'protocol': 'http', 'filename': 'index.ts'}
            ],
            'video_map': 'v:0?',
            'audio_map': 'a:0?',
        }
        resp = req.post(f"{self.base_url}/api/stream/add", headers=self._headers(), json=json_data, timeout=30)
        return resp.text

    def update_stream_url(self, name, url):
        resp = req.post(
            f"{self.base_url}/api/stream/{name}/dynamic-url",
            headers=self._headers(),
            json=[url],
            timeout=30
        )
        return resp.text

    def start_stream(self, name):
        resp = req.post(f"{self.base_url}/api/stream/{name}/start", headers=self._headers_action(), timeout=30)
        return resp.text

    def stop_stream(self, name):
        resp = req.post(f"{self.base_url}/api/stream/{name}/stop", headers=self._headers_action(), timeout=30)
        return resp.text

    def delete_stream(self, name):
        resp = req.post(f"{self.base_url}/api/stream/{name}/delete", headers=self._headers(), timeout=30)
        return resp.text

    def push_stream(self, name, url):
        result = self.create_stream(name, url)
        if 'error' in result:
            try:
                err = json.loads(result)
                if 'exists' in err.get('error', ''):
                    self.start_stream(name)
                    self.update_stream_url(name, url)
                    return 'updated'
                else:
                    return f"error: {err['error']}"
            except:
                return f"error: {result}"
        self.start_stream(name)
        return 'created'


def push_all_streams(ipko, xaccel, channels):
    print(f"\n[{now()}] Pushing {len(channels)} channels to XAccel...")

    existing_stats = xaccel.fetch_stream_stats()
    existing_names = {s['name'] for s in existing_stats}

    created = 0
    updated = 0
    failed = 0

    for i, ch_data in enumerate(channels):
        ch = ch_data['channel']
        stream_name = f"{STREAM_PREFIX}{ch['title']}"

        try:
            url = ch_data.get('stream_url')
            if not url:
                url = ipko.get_stream_url(ch['id'])
                if not url:
                    failed += 1
                    print(f"  [{i+1}/{len(channels)}] {ch['title']}: NO STREAM")
                    continue

            result = xaccel.push_stream(stream_name, url)
            if result == 'created':
                created += 1
            elif result == 'updated':
                updated += 1
            else:
                failed += 1
                print(f"  [{i+1}/{len(channels)}] {ch['title']}: {result}")
                continue

            if (i + 1) % 20 == 0:
                print(f"  [{i+1}/{len(channels)}] Progress: {created} created, {updated} updated, {failed} failed")

            time.sleep(0.2)

        except Exception as e:
            failed += 1
            print(f"  [{i+1}/{len(channels)}] {ch['title']}: ERROR - {e}")

    print(f"[{now()}] Push complete: {created} created, {updated} updated, {failed} failed")
    return created + updated


def check_stream_health(xaccel):
    stats = xaccel.fetch_stream_stats()
    ipko = [s for s in stats if s['name'].startswith(STREAM_PREFIX)]

    running = [s for s in ipko if s['status'] == 'running' and s['bitrate'] > 50]
    starting = [s for s in ipko if s['status'] == 'starting']
    stopped = [s for s in ipko if s['status'] == 'stopped']
    errored = [s for s in ipko if s['errors'] > 10]

    print(f"[{now()}] Health: {len(running)} running, {len(starting)} starting, {len(stopped)} stopped, {len(errored)} high-error")
    return {
        'total': len(ipko),
        'running': len(running),
        'starting': len(starting),
        'stopped': len(stopped),
        'problem_streams': stopped + [s for s in ipko if s['status'] == 'starting'],
    }


def fix_problem_streams(ipko, xaccel, problem_streams):
    if not problem_streams:
        return 0

    fixed = 0
    for s in problem_streams:
        name = s['name']
        ch_title = name.replace(STREAM_PREFIX, '')

        try:
            ch_id = None
            streams_file = os.path.join(WORK_DIR, 'streams.json')
            if os.path.exists(streams_file):
                with open(streams_file) as f:
                    stream_data = json.load(f)
                    for sd in stream_data['streams']:
                        if sd['channel']['title'] == ch_title:
                            ch_id = sd['channel']['id']
                            break

            if not ch_id:
                print(f"  Can't find channel ID for: {ch_title}")
                continue

            url = ipko.get_stream_url(ch_id)
            if not url:
                print(f"  No stream URL for: {ch_title}")
                continue

            xaccel.delete_stream(name)
            time.sleep(0.5)
            xaccel.create_stream(name, url)
            xaccel.start_stream(name)
            fixed += 1
            print(f"  Fixed: {ch_title}")
            time.sleep(0.5)

        except Exception as e:
            print(f"  Error fixing {ch_title}: {e}")

    return fixed


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else 'setup'

    ipko = IPKOSession()
    xaccel = XAccelClient()

    if not ipko.login():
        print(f"[{now()}] Failed to login!")
        sys.exit(1)

    if mode == 'setup':
        print(f"\n[{now()}] === IPKO TV Setup Mode ===")

        channels = ipko.get_channels()
        print(f"[{now()}] Found {len(channels)} channels")

        print(f"[{now()}] Getting stream URLs...")
        stream_data = []
        for i, ch in enumerate(channels):
            try:
                url = ipko.get_stream_url(ch['id'])
                if url:
                    stream_data.append({'channel': ch, 'stream_url': url})
                else:
                    print(f"  [{i+1}/{len(channels)}] {ch['title']}: NO STREAM")
                time.sleep(0.3)
            except Exception as e:
                print(f"  [{i+1}/{len(channels)}] {ch['title']}: ERROR - {e}")

            if (i + 1) % 30 == 0:
                print(f"  [{i+1}/{len(channels)}] {len(stream_data)} streams OK so far...")

        with open(os.path.join(WORK_DIR, 'streams.json'), 'w') as f:
            json.dump({
                'timestamp': now(),
                'total': len(channels),
                'ok': len(stream_data),
                'streams': stream_data,
            }, f, indent=2)
        print(f"[{now()}] {len(stream_data)} streams saved to streams.json")

        push_all_streams(ipko, xaccel, stream_data)

        time.sleep(30)
        health = check_stream_health(xaccel)
        print(f"\n[{now()}] === Setup Complete ===")
        print(f"Total: {health['total']}, Running: {health['running']}")

    elif mode == 'monitor':
        print(f"\n[{now()}] === IPKO TV Monitor Mode ===")
        cycle = 0
        while True:
            cycle += 1
            try:
                ipko.refresh_if_needed()

                health = check_stream_health(xaccel)

                if health['problem_streams']:
                    print(f"[{now()}] Fixing {len(health['problem_streams'])} problem streams...")
                    fixed = fix_problem_streams(ipko, xaccel, health['problem_streams'])
                    print(f"[{now()}] Fixed {fixed} streams")

                if cycle % 24 == 0:
                    print(f"[{now()}] Refreshing all stream URLs...")
                    streams_file = os.path.join(WORK_DIR, 'streams.json')
                    if os.path.exists(streams_file):
                        with open(streams_file) as f:
                            stream_data = json.load(f)

                        refreshed = 0
                        for sd in stream_data['streams']:
                            ch = sd['channel']
                            name = f"{STREAM_PREFIX}{ch['title']}"
                            try:
                                url = ipko.get_stream_url(ch['id'])
                                if url:
                                    xaccel.update_stream_url(name, url)
                                    refreshed += 1
                                time.sleep(0.3)
                            except:
                                pass
                        print(f"[{now()}] Refreshed {refreshed} stream URLs")

                print(f"[{now()}] Sleeping 30 min (cycle {cycle})...")
                time.sleep(1800)

            except KeyboardInterrupt:
                print(f"\n[{now()}] Stopping monitor...")
                break
            except Exception as e:
                print(f"[{now()}] Error in monitor cycle: {e}")
                traceback.print_exc()
                time.sleep(300)

    elif mode == 'refresh':
        print(f"\n[{now()}] === Refreshing stream URLs ===")
        streams_file = os.path.join(WORK_DIR, 'streams.json')
        if os.path.exists(streams_file):
            with open(streams_file) as f:
                stream_data = json.load(f)
        else:
            print("No streams.json found. Run 'setup' first.")
            sys.exit(1)

        refreshed = 0
        for sd in stream_data['streams']:
            ch = sd['channel']
            name = f"{STREAM_PREFIX}{ch['title']}"
            try:
                url = ipko.get_stream_url(ch['id'])
                if url:
                    xaccel.update_stream_url(name, url)
                    sd['stream_url'] = url
                    refreshed += 1
                time.sleep(0.3)
            except Exception as e:
                print(f"  Error refreshing {ch['title']}: {e}")

        with open(streams_file, 'w') as f:
            json.dump(stream_data, f, indent=2)
        print(f"[{now()}] Refreshed {refreshed} streams")

    elif mode == 'status':
        health = check_stream_health(xaccel)
        stats = xaccel.fetch_stream_stats()
        ipko_streams = [s for s in stats if s['name'].startswith(STREAM_PREFIX)]
        for s in sorted(ipko_streams, key=lambda x: x['name']):
            status = 'OK' if s['status'] == 'running' and s['bitrate'] > 50 else 'FAIL'
            print(f"  [{status}] {s['name']}: {s['status']}, {s['bitrate']:.0f}kbps, {s['errors']} errors")

    elif mode == 'cleanup':
        print(f"[{now()}] Cleaning up all IPKO streams from XAccel...")
        stats = xaccel.fetch_stream_stats()
        ipko_streams = [s for s in stats if s['name'].startswith(STREAM_PREFIX)]
        for s in ipko_streams:
            xaccel.delete_stream(s['name'])
            print(f"  Deleted: {s['name']}")
        print(f"[{now()}] Deleted {len(ipko_streams)} IPKO streams")

    else:
        print(f"Usage: {sys.argv[0]} [setup|monitor|refresh|status|cleanup]")


if __name__ == '__main__':
    main()
