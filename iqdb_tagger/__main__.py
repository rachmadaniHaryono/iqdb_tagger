#! /usr/bin/env python3
# -*- coding: utf-8 -*-
"""main module."""
import os

import cfscrape
import click
import mechanicalsoup
import requests
import structlog
from bs4 import BeautifulSoup
from requests_futures.sessions import FuturesSession

from iqdb_tagger import models
from iqdb_tagger.__init__ import db_version
from iqdb_tagger.custom_parser import get_tags as get_tags_from_parser
from iqdb_tagger.utils import default_db_path, thumb_folder, user_data_dir

db = '~/images/! tagged'
DEFAULT_SIZE = 150, 150
DEFAULT_PLACE = 'iqdb'
minsim = 75
services = ['1', '2', '3', '4', '5', '6', '10', '11']
forcegray = False
log = structlog.getLogger()
iqdb_url_dict = {
    'iqdb': ('http://iqdb.org', models.ImageMatch.SP_IQDB),
    'danbooru': (
        'http://danbooru.iqdb.org',
        models.ImageMatch.SP_DANBOORU
    ),
}


def get_page_result(image, url, browser=None):
    """Get page result.

    Args:
        image: Image path to be uploaded.
    Returns:
        HTML page from the result.
    """
    # compatibility
    br = browser

    if br is None:
        br = mechanicalsoup.StatefulBrowser(soup_config={'features': 'lxml'})
        br.raise_on_404 = True

    br.open(url)
    html_form = br.select_form('form')
    html_form.input({'file': image})
    br.submit_selected()
    # if ok, will output: <Response [200]>
    return br.get_current_page()


def get_posted_image(
        img_path, resize=False, size=None, output_thumb_folder=None):
    """Get posted image."""
    if output_thumb_folder is None:
        output_thumb_folder = thumb_folder

    img, _ = models.ImageModel.get_or_create_from_path(img_path)
    def_thumb_rel, _ = models.ThumbnailRelationship.get_or_create_from_image(
        image=img, thumb_folder=output_thumb_folder, size=DEFAULT_SIZE)
    resized_thumb_rel = None

    if resize and size:
        resized_thumb_rel, _ = \
            models.ThumbnailRelationship.get_or_create_from_image(
                image=img, thumb_folder=output_thumb_folder, size=size
            )
    elif resize:
        # use thumbnail if no size is given
        resized_thumb_rel = def_thumb_rel
    else:
        log.debug('Unknown config.', resize=resize, size=size)

    return resized_thumb_rel.thumbnail \
        if resized_thumb_rel is not None else img


def init_program(db_path=None):
    """Init program."""
    # create user data dir
    if not os.path.isdir(user_data_dir):
        os.makedirs(user_data_dir, exist_ok=True)
        log.debug('User data dir created.')
    # create thumbnail folder
    if not os.path.isdir(thumb_folder):
        os.makedirs(thumb_folder, exist_ok=True)
        log.debug('Thumbnail folder created.')

    # database
    if db_path is None:
        db_path = default_db_path
    models.init_db(db_path, db_version)


def get_tags(match_result, browser=None, scraper=None):
    """Get tags."""
    # compatibility
    br = browser

    if br is None:
        br = mechanicalsoup.StatefulBrowser(soup_config={'features': 'lxml'})
        br.raise_on_404 = True
    if scraper is None:
        scraper = cfscrape.CloudflareScraper()

    url = match_result.link
    br.open(url, timeout=10)
    page = br.get_current_page()
    tags = get_tags_from_parser(page, url, scraper)
    if tags:
        for tag in tags:
            namespace, tag_name = tag
            tag_model, _ = models.Tag.get_or_create(
                name=tag_name, namespace=namespace)
            models.MatchTagRelationship.get_or_create(
                match=match_result, tag=tag_model)
            yield tag_model
    else:
        log.debug('No tags found.')


def get_tags_with_async(match_results, browser=None, scraper=None, session=None):
    """Get tags with requests futures."""
    # compatibility
    br = browser
    if session is None:
        log.error('Require session.')
        return match_results
    if br is None:
        br = mechanicalsoup.StatefulBrowser(soup_config={'features': 'lxml'})
        br.raise_on_404 = True
    if scraper is None:
        scraper = cfscrape.CloudflareScraper()

    result = []
    page_set = []
    filtered_hosts = ['anime-pictures.net', 'www.theanimegallery.com']
    for result_dict in match_results:
        url = result_dict['url']
        from urllib.parse import urlparse
        if urlparse(url).netloc in filtered_hosts:
            log.debug(
                'URL in filtered hosts, no tag fetched', url=url)
            yield result_dict
            continue
        future_resp = session.get(result_dict['url'], timeout=10)
        resp = future_resp.result()
        page = BeautifulSoup(resp.content, 'lxml')
        tags = get_tags_from_parser(page, url, scraper)
        tags_models = []
        if tags:
            for tag in tags:
                namespace, tag_name = tag
                tag_model, _ = models.Tag.get_or_create(
                    name=tag_name, namespace=namespace)
                models.MatchTagRelationship.get_or_create(
                    match=result_dict['match_result'], tag=tag_model)
                tags_models.append(tag_model)
        else:
            log.debug('No tags found.')
        result_dict['tags'] = tags_models
        yield result_dict


def run_program_for_single_img(
        image, resize, size, place, match_filter, write_tags, browser,
        scraper, disable_tag_print=False, session=None
):
    """Run program for single image."""
    # compatibility
    br = browser

    post_img = get_posted_image(img_path=image, resize=resize, size=size)
    tag_textfile = image + '.txt'

    result = []
    for img_m_rel_set in post_img.imagematchrelationship_set:
        for item_set in img_m_rel_set.imagematch_set:
            if item_set.search_place_verbose == place:
                result.append(item_set)

    if not result:
        url, im_place = iqdb_url_dict[place]
        page = get_page_result(image=post_img.path, url=url, browser=br)
        # if ok, will output: <Response [200]>
        result = list(models.ImageMatch.get_or_create_from_page(
            page=page, image=post_img, place=im_place))
        result = [x[0] for x in result]

    if match_filter == 'best-match':
        result = [x for x in result if x.status == x.STATUS_BEST_MATCH]

    MatchTagRelationship = models.MatchTagRelationship
    use_async = True
    result_set = []
    for item in result:
        # type item: models.ImageMatch
        # type match_result: models.Match object
        result_dict = {
            'image_match': item,
            'match_result': item.match.match_result,
            'url': item.match.match_result.link,
            'tags': [],
        }
        match_result = item.match.match_result
        url = match_result.link

        mt_rel = MatchTagRelationship.select().where(
            MatchTagRelationship.match == result_dict['match_result'])
        tags = [x.tag for x in mt_rel]
        result_dict['tags'] = tags
        result_set.append(result_dict)

    new_result_set = []
    if use_async:
        new_result_set = list(get_tags_with_async(result_set, br, scraper, session))
    else:
        for result_dict in result_set:
            if not result_dict['tags']:
                tags = []
                try:
                    tags = list([x for x in get_tags(
                        result_dict['image_match'].match_result, br, scraper)])
                except requests.exceptions.ConnectionError as e:
                    log.error(str(e), url=url)
                result_dict['tags'] = tags
            new_result_set = result_dict

    for result_dict in new_result_set:
        print('{}|{}|{}'.format(
            result_dict['image_match'].similarity,
            result_dict['image_match'].status_verbose,
            result_dict['url']
        ))

        tags_verbose = [x.full_name for x in result_dict['tags']]
        log.debug('{} tag(s) founds'.format(len(tags_verbose)))
        if tags_verbose and not disable_tag_print:
            print('\n'.join(tags_verbose))
        else:
            log.debug('No printing tags.')

        if tags_verbose and write_tags:
            with open(tag_textfile, 'a') as f:
                f.write('\n'.join(tags_verbose))
                f.write('\n')
            log.debug('tags written')


@click.command()
@click.option(
    '--place', type=click.Choice(['iqdb', 'danbooru']),
    default=DEFAULT_PLACE,
    help='Specify iqdb place, default:{}'.format(DEFAULT_PLACE)
)
@click.option('--resize', is_flag=True, help='Use resized image.')
@click.option('--size', is_flag=True, help='Specify resized image.')
@click.option('--db-path', help='Specify Database path.')
@click.option(
    '--match-filter', type=click.Choice(['default', 'best-match']),
    default='default', help='Filter the result.'
)
@click.option(
    '--write-tags', is_flag=True, help='Write best match\'s tags to text.')
@click.option(
    '--input-mode', type=click.Choice(['default', 'folder']),
    default='default', help='Set input mode.'
)
@click.argument('prog-input')
def main(
    prog_input, resize=False, size=None,
    db_path=None, place=DEFAULT_PLACE, match_filter='default',
    write_tags=False, input_mode='default'
):
    """Get similar image from iqdb."""
    init_program(db_path)
    br = mechanicalsoup.StatefulBrowser(soup_config={'features': 'lxml'})
    br.raise_on_404 = True
    scraper = cfscrape.CloudflareScraper()
    session = FuturesSession()

    if input_mode == 'folder':
        assert os.path.isdir(prog_input), 'Input is not valid folder'
        files = [os.path.join(prog_input, x) for x in os.listdir(prog_input)]
        if not files:
            print('No files found.')
            return
        err_set = []
        err_found = False
        for idx, ff in enumerate(files):
            log.debug('file', f=ff, idx=idx, total=len(files))
            try:
                run_program_for_single_img(
                    ff, resize, size, place, match_filter, write_tags,
                    browser=br, scraper=scraper, disable_tag_print=True,
                    session=session
                )
            except Exception as e:  # pylint:disable=broad-except
                err_set.append((ff, e))
                err_found = True
        if err_found:
            print('Found error(s)')
            list(map(
                lambda x: print('path:{}\nerror:{}\n'.format(x[0], x[1])),
                err_set
            ))
    else:
        image = prog_input
        run_program_for_single_img(
            image, resize, size, place, match_filter, write_tags,
            browser=br, scraper=scraper, session=session
        )
