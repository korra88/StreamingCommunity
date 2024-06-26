# 18.04.24

import os
import sys
import time
import queue
import threading
import logging
import binascii
from queue import PriorityQueue
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor


# External libraries
import requests
from requests.exceptions import HTTPError, ConnectionError, Timeout, RequestException
from tqdm import tqdm


# Internal utilities
from Src.Util.console import console
from Src.Util.headers import get_headers
from Src.Util.color import Colors
from Src.Util._jsonConfig import config_manager


# Logic class
from ..M3U8 import (
    M3U8_Decryption,
    M3U8_Ts_Estimator,
    M3U8_Parser,
    M3U8_UrlFix
)
from .proxyes import main_test_proxy


# Warning
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)


# Config
TQDM_MAX_WORKER = config_manager.get_int('M3U8_DOWNLOAD', 'tdqm_workers')
TQDM_DELAY_WORKER = config_manager.get_float('M3U8_DOWNLOAD', 'tqdm_delay')
TQDM_USE_LARGE_BAR = config_manager.get_int('M3U8_DOWNLOAD', 'tqdm_use_large_bar')
REQUEST_TIMEOUT = config_manager.get_float('REQUESTS', 'timeout')
THERE_IS_PROXY_LIST = len(config_manager.get_list('REQUESTS', 'proxy')) > 0


# Variable
headers_index = config_manager.get_dict('REQUESTS', 'index')
headers_segments = config_manager.get_dict('REQUESTS', 'segments')
session = requests.Session()
session.verify = config_manager.get_bool('REQUESTS', 'verify_ssl')


class M3U8_Segments:
    def __init__(self, url: str, tmp_folder: str):
        """
        Initializes the M3U8_Segments object.

        Args:
            - url (str): The URL of the M3U8 playlist.
            - tmp_folder (str): The temporary folder to store downloaded segments.
        """
        self.url = url
        self.tmp_folder = tmp_folder
        self.tmp_file_path = os.path.join(self.tmp_folder, "0.ts")
        os.makedirs(self.tmp_folder, exist_ok=True)

        # Util class
        self.decryption: M3U8_Decryption = None 
        self.class_ts_estimator = M3U8_Ts_Estimator(0) 
        self.class_url_fixer = M3U8_UrlFix(url)

        # Sync
        self.queue = PriorityQueue()
        self.stop_event = threading.Event()
        
    def __get_key__(self, m3u8_parser: M3U8_Parser) -> bytes:
        """
        Retrieves the encryption key from the M3U8 playlist.

        Args:
            - m3u8_parser (M3U8_Parser): The parser object containing M3U8 playlist information.

        Returns:
            bytes: The encryption key in bytes.
        """
        headers_index['user-agent'] = get_headers()


        # Construct the full URL of the key
        key_uri = urljoin(self.url, m3u8_parser.keys.get('uri'))  
        logging.info(f"Uri key: {key_uri}")

        try:
            response = requests.get(key_uri, headers=headers_index)
            response.raise_for_status()

        except Exception as e:
            raise Exception(f"Failed to fetch key from {key_uri}: {e}")

        # Convert the content of the response to hexadecimal and then to bytes
        hex_content = binascii.hexlify(response.content).decode('utf-8')
        byte_content = bytes.fromhex(hex_content)
        
        logging.info(f"Key: ('hex': {hex_content}, 'byte': {byte_content})")
        return byte_content

    def parse_data(self, m3u8_content: str) -> None:
        """
        Parses the M3U8 content to extract segment information.

        Args:
            - m3u8_content (str): The content of the M3U8 file.
        """
        m3u8_parser = M3U8_Parser()
        m3u8_parser.parse_data(uri=self.url, raw_content=m3u8_content)

        console.log(f"[red]Expected duration after download: {m3u8_parser.get_duration()}")
        console.log(f"[red]There is key: [yellow]{m3u8_parser.keys is not None}")

        # Check if there is an encryption key in the playlis
        if m3u8_parser.keys is not None:
            try:

                # Extract byte from the key
                key = self.__get_key__(m3u8_parser)
                
            except Exception as e:
                raise Exception(f"Failed to retrieve encryption key {e}.")

            iv = m3u8_parser.keys.get('iv')
            method = m3u8_parser.keys.get('method')

            # Create a decryption object with the key and set the method
            self.decryption = M3U8_Decryption(key, iv, method)

        # Store the segment information parsed from the playlist
        self.segments = m3u8_parser.segments

        # Fix URL if it is incomplete (missing 'http')
        for i in range(len(self.segments)):
            segment_url = self.segments[i]

            if "http" not in segment_url:
                self.segments[i] = self.class_url_fixer.generate_full_url(segment_url)
                logging.info(f"Generated new URL: {self.segments[i]}, from: {segment_url}")

        # Update segments for estimator
        self.class_ts_estimator.total_segments = len(self.segments)
        logging.info(f"Segmnets to donwload: [{len(self.segments)}]")

        # Proxy
        if THERE_IS_PROXY_LIST:
            console.log("[red]Validate proxy.")
            self.valid_proxy = main_test_proxy(self.segments[0])
            console.log(f"[cyan]N. Valid ip: [red]{len(self.valid_proxy)}")

            if len(self.valid_proxy) == 0:
                sys.exit(0)

    def get_info(self) -> None:
        """
        Makes a request to the index M3U8 file to get information about segments.
        """
        headers_index['user-agent'] = get_headers()

        # Send a GET request to retrieve the index M3U8 file
        response = requests.get(self.url, headers=headers_index)
        response.raise_for_status()

        # Save the M3U8 file to the temporary folder
        if response.ok:
            path_m3u8_file = os.path.join(self.tmp_folder, "playlist.m3u8")
            open(path_m3u8_file, "w+").write(response.text) 

        # Parse the text from the M3U8 index file
        self.parse_data(response.text)  

    def make_requests_stream(self, ts_url: str, index: int, progress_bar: tqdm) -> None:
        """
        Downloads a TS segment and adds it to the segment queue.

        Args:
            - ts_url (str): The URL of the TS segment.
            - index (int): The index of the segment.
            - progress_bar (tqdm): Progress counter for tracking download progress.
        """
        try:

            # Generate headers
            start_time = time.time()
            headers_segments['user-agent'] = get_headers()

            # Make request to get content
            if THERE_IS_PROXY_LIST:
                proxy = self.valid_proxy[index % len(self.valid_proxy)]
                logging.info(f"Use proxy: {proxy}")
                response = session.get(ts_url, headers=headers_segments, timeout=REQUEST_TIMEOUT, proxies=proxy)
            else:
                response = session.get(ts_url, headers=headers_segments, timeout=REQUEST_TIMEOUT)

            # Get response content
            response.raise_for_status()
            segment_content = response.content

            # Update bar
            duration = time.time() - start_time
            response_size = int(response.headers.get('Content-Length', 0))
            self.class_ts_estimator.update_progress_bar(response_size, duration, progress_bar)
            
            # Decrypt the segment content if decryption is needed
            if self.decryption is not None:
                segment_content = self.decryption.decrypt(segment_content)

            # Add the segment to the queue
            self.queue.put((index, segment_content))
            progress_bar.update(1)

        except (HTTPError, ConnectionError, Timeout, RequestException) as e:
            progress_bar.update(1)
            logging.error(f"Request-related exception while downloading segment: {e}")

        except Exception as e:
            progress_bar.update(1)
            logging.error(f"An unexpected exception occurred while download segment: {e}")

    def write_segments_to_file(self):
        """
        Writes downloaded segments to a file in the correct order.
        """
        with open(self.tmp_file_path, 'wb') as f:
            expected_index = 0
            buffer = {}

            while not self.stop_event.is_set() or not self.queue.empty():
                try:
                    index, segment_content = self.queue.get(timeout=1)

                    if index == expected_index:
                        f.write(segment_content)
                        f.flush()
                        expected_index += 1

                        # Write any buffered segments in order
                        while expected_index in buffer:
                            f.write(buffer.pop(expected_index))
                            f.flush()
                            expected_index += 1
                    else:
                        buffer[index] = segment_content

                except queue.Empty:
                    continue

    def download_streams(self, add_desc):
        """
        Downloads all TS segments in parallel and writes them to a file.

        Args:
            - add_desc (str): Additional description for the progress bar.
        """
        if TQDM_USE_LARGE_BAR:
            bar_format=f"{Colors.YELLOW}Downloading {Colors.WHITE}({add_desc}{Colors.WHITE}): {Colors.RED}{{percentage:.2f}}% {Colors.MAGENTA}{{bar}} {Colors.WHITE}[ {Colors.YELLOW}{{n_fmt}}{Colors.WHITE} / {Colors.RED}{{total_fmt}} {Colors.WHITE}] {Colors.YELLOW}{{elapsed}} {Colors.WHITE}< {Colors.CYAN}{{remaining}}{{postfix}} {Colors.WHITE}]"
        else:
            bar_format=f"{Colors.YELLOW}Proc{Colors.WHITE}: {Colors.RED}{{percentage:.2f}}% {Colors.WHITE}| {Colors.CYAN}{{remaining}}{{postfix}} {Colors.WHITE}]"

        # Create progress bar
        progress_bar = tqdm(
            total=len(self.segments), 
            unit='s',
            ascii='░▒█',
            bar_format=bar_format
        )

        # Start a separate thread to write segments to the file
        writer_thread = threading.Thread(target=self.write_segments_to_file)
        writer_thread.start()

        # Start all workers
        with ThreadPoolExecutor(max_workers=TQDM_MAX_WORKER) as executor:
            for index, segment_url in enumerate(self.segments):
                time.sleep(TQDM_DELAY_WORKER)
                executor.submit(self.make_requests_stream, segment_url, index, progress_bar)

        # Wait for all tasks to complete
        executor.shutdown(wait=True)
        self.stop_event.set()
        writer_thread.join()
        progress_bar.close()
