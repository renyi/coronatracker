#!/usr/bin/env python3
#
# -*- coding: utf-8 -*-
#
# Last update: 31/01/2020
# Authors:
#   - dipto.pratyaksa@carltondigital.com.au
#   - samueljklee@gmail.com
#
# REF:
# https://santhoshveer.com/rss-feed-reader-using-python/
# https://medium.com/@ankurjain_79625/how-did-i-scrape-news-article-using-python-6eff936b3c8c
# https://medium.com/@randerson112358/scrape-summarize-news-articles-using-python-51a48af1b4e2
#
# TO DO:
# Store the relevant RSS feed into shared repo, like Google sheet
# Algo to extract the casualty stats from linked news article
#
# USAGE:
# python scrape_rss.py -c -d -v
#   -v : verbose, show some log messages. default=False
#   -d : debug mode, write to output.jsonl, else, write to db. default=True
#   -c : clear cache, default=False
#   -a : get all, skip cache. api uses this to crawl everything
#        - update database doesn't use this, to prevent duplicated entries
#
# Example:
#   - write to db with log messages, doesn't update ./data/<lang>/output.jsonl
#       - python scrape_rss.py -v       # writes to test table
#       - python scrape_rss.py -v -p    # writes to production table
#   - server runs api endpoint that reads from ./data/<lang>/output.jsonl
#       to show all latest news without log messages, skip cache as well
#       - python scrape_rss.py -d -a
#

from urllib.request import urlopen, Request
from bs4 import BeautifulSoup
from datetime import datetime, timezone
from dateutil.parser import parse
import re

import nltk
from newspaper import Article
import threading
import queue

import argparse
import json
import os

import db_connector

"""
https://www.theage.com.au/rss/feed.xml
https://www.theage.com.au/rss/world.xml
# http://www.heraldsun.com.au/news/breaking-news/rss
# http://www.heraldsun.com.au/rss
https://www.news.com.au/content-feeds/latest-news-world/
https://www.news.com.au/content-feeds/latest-news-national/
http://www.dailytelegraph.com.au/news/breaking-news/rss
http://www.dailytelegraph.com.au/news/national/rss
http://www.dailytelegraph.com.au/newslocal/rss
http://www.dailytelegraph.com.au/news/world/rss
https://www.sbs.com.au/news/topic/latest/feed
https://www.channelnewsasia.com/googlenews/cna_news_sitemap.xml
"""

# some sitemap contains different attributes
NEWS_URLs = {
    "en": [
        (
            "https://www.scmp.com/rss/318208/feed",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "https://www.theage.com.au/rss/feed.xml",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "https://www.theage.com.au/rss/world.xml",
            {"title": "title", "description": "description", "url": "link",},
        ),
# Remove heraldsun rss to prevent scraping the same content as other rss
# > as it's a smaller newspaper that is likely syndicating news from bigger news        
#         (
#             "http://www.heraldsun.com.au/news/breaking-news/rss",
#             {"title": "title", "description": "description", "url": "link",},
#         ),
#         (
#             "http://www.heraldsun.com.au/rss",
#             {"title": "title", "description": "description", "url": "link",},
#         ),
        (
            "https://www.news.com.au/content-feeds/latest-news-world/",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "https://www.news.com.au/content-feeds/latest-news-national/",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "http://www.dailytelegraph.com.au/news/breaking-news/rss",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "http://www.dailytelegraph.com.au/news/national/rss",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "http://www.dailytelegraph.com.au/newslocal/rss",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "http://www.dailytelegraph.com.au/news/world/rss",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "https://www.sbs.com.au/news/topic/latest/feed",
            {"title": "title", "description": "description", "url": "link",},
        ),
        (
            "https://www.channelnewsasia.com/googlenews/cna_news_sitemap.xml",
            {"title": "title", "description": "news:keywords", "url": "loc", "publish_date" : "news:publication_date"},
        ),
    ]
}

global READ_ALL_SKIP_CACHE
global WRITE_TO_PROD_TABLE
global WRITE_TO_DB_MODE
global VERBOSE

CACHE_FILE = "cache.txt"
OUTPUT_FILENAME = "output.jsonl"

# "Sat, 25 Jan 2020 01:52:22 +0000"
DATE_RFC_2822_REGEX_RULE = (
    r"[\d]{1,2} [ADFJMNOS]\w* [\d]{4} \b(?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] [\+]{1}[0-9]{4}\b"
)
DATE_RFC_2822_DATE_FORMAT = "%d %b %Y %H:%M:%S %z"
# ISO 8601 | 2020-01-31T22:10:38+0800
DATE_ISO_8601_REGEX_RULE = r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}\+[0-9]{2}\:?[0-9]{2}"
ISO_8601_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"
DATE_FORMAT = "%Y-%m-%d %H:%M:%S"

CORONA_KEYWORDS = set(["corona", "coronavirus"])
THREAD_LIMIT = 10

CACHE = set()
THREADS = []
XML_QUEUE = queue.Queue()
EXTRACT_FEED_QUEUE = queue.Queue()
RSS_STACK = {}


def news():
    while not XML_QUEUE.empty():
        try:
            lang, root_url_schema = XML_QUEUE.get()
        except queue.Empty:
            if VERBOSE:
                print("Root/xml queue is empty")
            return
        root_url, schema = root_url_schema
        hdr = {"User-Agent": "Mozilla/5.0"}
        req = Request(root_url, headers=hdr)
        parse_xml_url = urlopen(req)
        xml_page = parse_xml_url.read()
        parse_xml_url.close()

        soup_page = BeautifulSoup(xml_page, "xml")
        news_list = soup_page.findAll("item")

        if not news_list:
            news_list = soup_page.findAll("url")

        for getfeed in news_list:
            EXTRACT_FEED_QUEUE.put((lang, root_url, soup_page, getfeed, schema))


def extract_feed_data():
    while not EXTRACT_FEED_QUEUE.empty():
        try:
            lang, root_url, soup_page, feed_source, schema = EXTRACT_FEED_QUEUE.get()
        except queue.Empty:
            if VERBOSE:
                print("Feed Queue is empty")
            return

        # Extract from xml
        res_title = feed_source.find(schema["title"]).text
        res_desc = feed_source.find(schema["description"]).text

        # check if any of the CORONA_KEYWORDS occur in title or description
        if (
            len(
                set(re.findall(r"\w+", res_title.lower())).intersection(
                    CORONA_KEYWORDS
                )
            )
            == 0
            and len(
                set(re.findall(r"\w+", res_desc.lower())).intersection(
                    CORONA_KEYWORDS
                )
            )
            == 0
        ):
            continue

        rss_record = {}
        rss_record["title"] = res_title
        rss_record["url"] = feed_source.find(schema["url"]).text

        if rss_record["url"] in CACHE:
            continue

        if not READ_ALL_SKIP_CACHE:
            add_to_cache(rss_record["url"])

        rss_record["addedOn"] = datetime.utcnow().strftime(DATE_FORMAT)
        # rss_record["source"] = soup_page.channel.title.text

        article = extract_article(rss_record["url"])

        # Overwrite description if exists in meta tag
        if (
            "og" in article.meta_data
            and "description" in article.meta_data["og"]
            and len(article.meta_data["og"]["description"])
        ):
            rss_record["description"] = article.meta_data["og"]["description"]
        else:
            rss_record["description"] = res_desc

        # Get language
        rss_record["language"] = article.meta_lang

        # Get siteName
        rss_record["siteName"] = re.sub(
            r"https?://(www\.)?", "", article.source_url
        )

        # Get the authors
        rss_record["author"] = ", ".join(article.authors)

        # Get the publish date
        if "publish_date" in schema:
            rss_record["publishedAt"] = date_convert(feed_source.find(schema["publish_date"]).text)
        elif feed_source.pubDate:
            rss_record["publishedAt"] = date_convert(feed_source.pubDate.text)
        elif article.publish_date:
            rss_record["publishedAt"] = article.publish_date.strftime(DATE_FORMAT)
        elif (
            "article" in article.meta_data
            and "modified_time" in article.meta_data["article"]
        ):
            rss_record["publishedAt"] = date_convert(
                article.meta_data["article"]["modified_time"]
            )
        elif soup_page.lastBuildDate:
            rss_record["publishedAt"] = date_convert(soup_page.lastBuildDate.text)
        else:
            rss_record["publishedAt"] = ""

        rss_record["content"] = article.text
        # Get the top image
        rss_record["urlToImage"] = article.top_image

        if lang not in RSS_STACK:
            RSS_STACK[lang] = []
        RSS_STACK[lang].append(rss_record)


def print_pretty():
    for lang, rss_records in RSS_STACK.items():
        for rss_record in rss_records:
            to_print = ""
            to_print += "\ntitle:\t" + rss_record["title"]
            to_print += "\ndescription:\t" + rss_record["description"]
            to_print += "\nurl:\t" + rss_record["url"]
            to_print += "\npublishedAt:\t" + rss_record["publishedAt"]
            to_print += "\naddedOn:\t" + rss_record["addedOn"]
            to_print += "\nauthor:\t" + rss_record["author"]
            to_print += "\ncontent:\n" + rss_record["content"]
            to_print += "\nurlToImage:\t" + rss_record["urlToImage"]
            to_print += "\nlanguage:\t" + rss_record["language"]
            to_print += "\nsiteName:\t" + rss_record["siteName"]
            to_print += ""
            try:
                if VERBOSE:
                    print(to_print.expandtabs())
            except:
                pass


def write_output():
    for lang, rss_records in RSS_STACK.items():
        with open("data/{}/output.jsonl".format(lang), "w") as fh:
            for rss_record in rss_records:
                json.dump(rss_record, fh)
                fh.write("\n")


def save_to_db():
    db_connector.connect()
    for lang, rss_records in RSS_STACK.items():
        for rss_record in rss_records:
            db_connector.insert(rss_record, "prod" if WRITE_TO_PROD_TABLE else "test")


def date_convert(date_string):
    if VERBOSE:
        print("input date: " + date_string)

    if len(re.findall(DATE_RFC_2822_REGEX_RULE, date_string,)) > 0:
        match_dateformat = re.findall(DATE_RFC_2822_REGEX_RULE, date_string,)
        datetime_str = match_dateformat[0]
        original_datetime_format = datetime.strptime(datetime_str, DATE_RFC_2822_DATE_FORMAT)

    elif len(re.findall(DATE_ISO_8601_REGEX_RULE, date_string,)) > 0:
        # Fall back to try datetime ISO 8601 format
        match_dateformat = re.findall(DATE_ISO_8601_REGEX_RULE, date_string,)
        datetime_str = match_dateformat[0]
        original_datetime_format = datetime.strptime(datetime_str, ISO_8601_DATE_FORMAT)

    else:
        original_datetime_format = date_string

    datetime_object = original_datetime_format.astimezone(timezone.utc).strftime(DATE_FORMAT)
    return str(datetime_object)


def extract_article(link):
    if VERBOSE:
        print("Extracting from: ", link)
    article = Article(link)
    # Do some NLP
    article.download()  # Downloads the link's HTML content
    article.parse()  # Parse the article
    nltk.download("punkt")  # 1 time download of the sentence tokenizer
    article.nlp()  #  Keyword extraction wrapper
    return article


def parser():
    parser = argparse.ArgumentParser(description="Scrape XML sources")
    parser.add_argument("-v", "--verbose", action="store_true", help="Verbose")
    parser.add_argument("-d", "--debug", action="store_true", help="Debugging")
    parser.add_argument("-c", "--clear", action="store_true", help="Clear Cache")
    parser.add_argument("-p", "--production", action="store_true", help="Writes to production table")
    parser.add_argument("-a", "--all", action="store_true", help="Skip read and write on cache")
    return parser.parse_args()


def read_cache():
    with open(CACHE_FILE, "r") as fh:
        stream = fh.read()
        for row in stream.split("\n"):
            CACHE.add(row)


def add_to_cache(url):
    with open(CACHE_FILE, "a+") as fh:
        fh.write(url + "\n")
    CACHE.add(url)


# arguments
args = parser()

VERBOSE = args.verbose
READ_ALL_SKIP_CACHE = args.all
WRITE_TO_DB_MODE = not args.debug
WRITE_TO_PROD_TABLE = args.production

# create required folders
if not os.path.isdir("data"):
    os.mkdir("./data")

# reset cache
if args.clear:
    os.system("rm {}".format(CACHE_FILE))

# check cache file exists
if not os.path.isfile(CACHE_FILE):
    os.system("touch {}".format(CACHE_FILE))

# if set READ_ALL_SKIP_CACHE, skip reading cache
if not READ_ALL_SKIP_CACHE:
    read_cache()

# place initial xml urls to queue
for lang, all_rss in NEWS_URLs.items():
    if not os.path.isdir("./data/{}".format(lang)):
        os.mkdir("./data/{}".format(lang))
    for rss in all_rss:
        XML_QUEUE.put((lang, rss))

# extract all xml data
for i in range(THREAD_LIMIT):
    t = threading.Thread(target=news)
    t.start()
    THREADS.append(t)

for thread in THREADS:
    thread.join()

if VERBOSE:
    print("Done extracting all root urls")

# process all latest feed
for i in range(len(THREADS)):
    THREADS[i] = threading.Thread(target=extract_feed_data)
    THREADS[i].start()

for thread in THREADS:
    thread.join()

if VERBOSE:
    print("Done extracting all feed data")

if WRITE_TO_DB_MODE:
    # Store to DB
    save_to_db()
else:
    # print output and write to jsonl file
    print_pretty()
    write_output()

if VERBOSE:
    count = 0
    for lang, rss_records in RSS_STACK.items():
        count += len(rss_records)
    print("Total feeds: {}".format(count))
