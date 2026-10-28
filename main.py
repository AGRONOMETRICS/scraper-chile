import os
import re
import sys
import json
import functools
import argparse
from datetime import date, datetime
from typing import Dict, List, Optional

import pandas as pd
import dataset
import requests
import urllib3

from dotenv import find_dotenv, load_dotenv
from requests.exceptions import ConnectionError, HTTPError, Timeout

from config import settings, get_logger
from databases.db import (
    column_data_types,
    columns_specification,
    columns_to_preview,
    set_column_types,
    unique_together_keys,
    updatable_keys,
)
from summary import RunSummary
from utils import (
    align_dataframes,
    cast_dataframe,
    format_empty_dataframe,
    measure_time,
    parse_date,
    parse_season,
    run_sleep,
    send_mail,
    truncate_table,
)

# Setup
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
logger = get_logger(name='scraper_chile', log_filename='scraper_chile.log')

# Load environment variables
dotenv_path = find_dotenv()
load_dotenv(dotenv_path)

# Settings
REPORT_URL = settings.REPORT_URL
# How the summary names what this run read, e.g. 'reportes/decofrut'.
DATASET_LABEL = '/'.join(REPORT_URL.rstrip('/').rsplit('/', 2)[-2:])
DEBUG = settings.DEBUG
TEST_RESPONSE = settings.TEST_RESPONSE
CREATE_JSON_FILES = settings.CREATE_JSON_FILES

headers = {'Accept': 'application/json'}
urllib3.disable_warnings()

# Commodities this scraper covers, as our system names them, each with the
# specie the DecoFrut report files it under (exact match, as the API spells it).
COMMODITIES = [
    ('Blueberries', 'Arandanos'),
    ('Grapes', 'Uva De Mesa'),
]
COMMODITY_SPECIES = dict(COMMODITIES)
# The API matches `specie` ignoring case, so the lookup back does too.
SPECIE_COMMODITY = {specie.lower(): commodity for commodity, specie in COMMODITIES}


# --- Fetching ---------------------------------------------------------------

def slugify(value: str) -> str:
    return re.sub(r'[^A-Za-z0-9]+', '-', str(value)).strip('-').upper()


def response_file_path(season, specie, date_from, date_to, page) -> str:
    parent_directory = os.path.abspath('.')
    dir_name = os.path.join(parent_directory, 'data', season or 'all')
    file_name = (
        f'_response_{slugify(specie) if specie else "ALL"}'
        f'_from{date_from or "all"}_to{date_to or "all"}'
        f'_p{page:04d}.json'
    )
    return os.path.join(dir_name, file_name)


def request_page(season, specie, date_from, date_to, page) -> Optional[Dict]:
    """One HTTP call to the report, as the raw `{data, pagination}` payload.

    Reads the cached JSON file instead when TEST_RESPONSE is on, and writes that
    cache when CREATE_JSON_FILES is on. Returns None when the page could not be
    read, so the caller can tell a failure apart from an empty page.
    """
    params = {'page': page, 'pageSize': settings.PAGE_SIZE}
    if season:
        params['season'] = season
    if specie:
        params['specie'] = specie
    if date_from:
        params['fechaDesde'] = date_from
    if date_to:
        params['fechaHasta'] = date_to

    file_path = response_file_path(season, specie, date_from, date_to, page)
    label = f'{season or "all"}/{specie or "all"} p{page}'

    if TEST_RESPONSE:
        if not os.path.exists(file_path):
            logger.warning(f'[{label}] No cached response at {file_path}')
            return None
        with open(file_path, 'r', encoding='utf-8') as file:
            return json.load(file)

    attempts = settings.REQUEST_RETRIES + 1
    text = None
    for attempt in range(1, attempts + 1):
        try:
            r = requests.get(REPORT_URL, params=params, headers=headers,
                             auth=(settings.API_USERNAME, settings.API_PASSWORD),
                             timeout=settings.REQUEST_TIMEOUT)
            logger.info(r.url)
            if r.status_code in (400, 401, 403):
                # Bad credentials or filters: retrying will not change the answer.
                logger.error(f'[{label}] {r.status_code}: {r.text[:300]}')
                return None
            r.raise_for_status()
            text = r.text
            break
        except (ConnectionError, HTTPError, Timeout) as e:
            logger.error(f'[{label}] Request failed ({attempt}/{attempts}): {e}')
            if attempt < attempts:
                run_sleep(settings.REQUEST_DELAY * 5)

    if text is None:
        logger.error(f'[{label}] Giving up on {params}')
        return None

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as e:
        logger.error(f'[{label}] Response is not valid JSON: {e}')
        return None

    if not isinstance(payload, dict) or not isinstance(payload.get('data'), list):
        logger.error(f'[{label}] Unexpected response payload: {str(payload)[:200]}')
        return None

    if CREATE_JSON_FILES:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(text)

    return payload


def request_report(season, specie, date_from, date_to) -> Dict:
    """Walk every page of one (season, specie, date range) query.

    The API orders by ETD and rowId, so reading the pages in sequence neither
    repeats nor skips rows. The rows are only handed back when their count
    matches the `total` the server announced: a partial read would make every
    missing row look deleted.
    """
    rows = []
    total = None
    page = 1
    while True:
        if page > 1 and not TEST_RESPONSE:
            run_sleep(settings.REQUEST_DELAY)

        payload = request_page(season, specie, date_from, date_to, page)
        if payload is None:
            return {'requests_made': page, 'rows': None}

        data = payload['data']
        rows.extend(data)
        total = (payload.get('pagination') or {}).get('total', total)
        logger.info(f'Page {page}: {len(rows):,}/{total if total is not None else "?":,} rows')

        if not data or (total is not None and len(rows) >= total):
            break
        page += 1

    if total is not None and len(rows) != total:
        logger.error(f'Read {len(rows):,} rows but the server announced {total:,}; '
                     f'the report probably reloaded mid-run. Discarding this read.')
        return {'requests_made': page, 'rows': None}

    return {'requests_made': page, 'rows': rows}


def parse_record(raw: Dict) -> Dict:
    """Map one API row onto the column contract in databases/db.py.

    `rowId` is dropped on purpose: the API regenerates it on every daily load.
    """
    def text(value):
        return '' if value is None else str(value).strip()

    def number(value):
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    return {
        'commodity': SPECIE_COMMODITY.get(text(raw.get('specie')).lower(), ''),
        'season': text(raw.get('season')),
        'etd': text(raw.get('etd')),
        'etd_week': text(raw.get('etdweek')),
        'market': text(raw.get('market')),
        'country': text(raw.get('country')),
        'importer': text(raw.get('importer')),
        'exporter': text(raw.get('exporter')),
        'pol': text(raw.get('pol')),
        'pod': text(raw.get('pod')),
        'specie': text(raw.get('specie')),
        'variety': text(raw.get('variety')),
        'production_method': text(raw.get('productionmethod')),
        'cargo_type': text(raw.get('cargotype')),
        'vessel': text(raw.get('vessel')),
        'market_group': text(raw.get('marketgroup')),
        'region': text(raw.get('region')),
        'boxes': number(raw.get('boxes')),
        'kilograms': number(raw.get('kilograms')),

        # Extra field for ETL process
        'etl_processed': False,
    }


def split_items(raw_items: List[Dict]) -> Dict[str, Dict]:
    """Parse the rows and group them by season, the unit a run compares in.

    Returns, per season, the three row groups the comparison step needs:
      original_items   - every row the server reported, before value filtering
      processed_items  - the rows that carry an actual volume
      items_zero_value - the rows dropped for having no volume at all
    """
    groups: Dict[str, Dict] = {}
    for raw in raw_items:
        item = parse_record(raw)
        group = groups.setdefault(item['season'], {
            'original_items': [], 'processed_items': [], 'items_zero_value': []})

        group['original_items'].append(item.copy())

        # Prevent processing items with NO VALUE
        if item['boxes'] == 0 and item['kilograms'] == 0:
            group['items_zero_value'].append(item.copy())
            continue

        group['processed_items'].append(item)

    for season, group in groups.items():
        if group['items_zero_value']:
            logger.info(f'[{season}] {len(group["items_zero_value"])} items '
                        f'ignored (zero value)')
    return groups


# --- Scraping ---------------------------------------------------------------

def validate_table_columns(table):
    """Refuse to run if the stored table carries a column the contract ignores."""
    columns_required = list(columns_specification.keys())
    columns_not_found = [
        col for col in table.columns
        if col != 'id' and col not in columns_required
    ]
    if columns_not_found:
        msg = f'Columns not found: {columns_not_found}'
        logger.error(msg)
        raise Exception(msg)


def concat(frames):
    frames = [f for f in frames if not f.empty]
    if not frames:
        return pd.DataFrame([])
    return pd.concat(frames, ignore_index=True)


def diff_records(processed_items: pd.DataFrame, existing_records: pd.DataFrame):
    """Split the incoming rows against what is already stored.

    Compares on `unique_together_keys` (the natural key of a row) and looks at
    `updatable_keys` (the measured values) to decide what moved:

      new       - the key is not in the database yet
      updatable - the key is there but at least one value changed
      deletable - the database has a row the server no longer reports
    """
    processed_items, existing_records = align_dataframes(
        processed_items, existing_records, unique_together_keys)

    merged_df = processed_items.merge(
        existing_records[unique_together_keys + updatable_keys],
        on=unique_together_keys, how='left', suffixes=('_x', '_y'))

    # Check for entries to update
    conditions_updated_entry = []
    for key in updatable_keys:
        condition = merged_df[f'{key}_x'] != merged_df[f'{key}_y']
        conditions_updated_entry.append(condition)

    is_updated_entry = (
        functools.reduce(lambda a, b: a | b, conditions_updated_entry)
        & merged_df[[f'{key}_y' for key in updatable_keys]].notna().all(axis=1)
    )

    # check new entries from server
    conditions_new_entry = []
    for key in updatable_keys:
        condition = merged_df[f'{key}_y'].isna()
        conditions_new_entry.append(condition)

    is_new_entry = functools.reduce(lambda a, b: a & b, conditions_new_entry)

    # Reset index before applying boolean indexing
    processed_items.reset_index(drop=True, inplace=True)

    new_items = processed_items.loc[is_new_entry].copy()
    updatable_items = processed_items.loc[is_updated_entry].copy()

    # Deletable items from db (maybe removed from server)
    merged_for_deletion = existing_records.merge(
        processed_items[unique_together_keys],
        on=unique_together_keys,
        how='left',
        indicator=True,
    )
    deletable_records = (
        merged_for_deletion[merged_for_deletion['_merge'] == 'left_only']
        .drop(columns=['_merge'])
    )
    if 'id' in deletable_records.columns:
        deletable_records = deletable_records.drop_duplicates(subset='id')

    return new_items, updatable_items, deletable_records


def key_frame(dataframe: pd.DataFrame) -> pd.DataFrame:
    """The natural key of every row, as text, so both sides compare the same way."""
    return dataframe[unique_together_keys].astype(str)


def find_duplicated_records(existing_records: pd.DataFrame,
                            processed_items: pd.DataFrame) -> pd.DataFrame:
    """Stored rows that cannot be reached by a single-row update.

    Two cases end up here, and both are resolved by deleting the rows and
    letting them come back as new items:

    1. The database holds several rows under one natural key. An update keyed on
       it would write the same values over all of them.
    2. The response holds several rows under one natural key - two shipments of
       the same variety, exporter and vessel on the same ETD, say. Updating
       through the key would collapse them into whichever row was written last,
       losing the other one for good.
    """
    if existing_records.empty:
        return pd.DataFrame([])

    existing_keys = key_frame(existing_records)
    duplicated_in_db = existing_keys.duplicated(keep=False)

    incoming_keys = key_frame(processed_items)
    duplicated_incoming = set(
        incoming_keys[incoming_keys.duplicated(keep=False)]
        .itertuples(index=False, name=None)
    )
    duplicated_in_response = pd.Series(
        [key in duplicated_incoming
         for key in existing_keys.itertuples(index=False, name=None)],
        index=existing_records.index,
    )

    mask = duplicated_in_db | duplicated_in_response
    if not mask.any():
        return pd.DataFrame([])

    return existing_records[mask].sort_values(unique_together_keys, ascending=False)


@measure_time
def scrape(seasons=None, commodities=None, date_from=None, date_to=None,
           truncate=False, send_email_notifications=True):
    run_started = datetime.now()

    # Database connection
    uri = os.getenv('DATABASE_URL')
    if not uri:
        raise RuntimeError('DATABASE_URL not set in .env')
    if not TEST_RESPONSE and not (settings.API_USERNAME and settings.API_PASSWORD):
        raise RuntimeError('USERNAME / PASSWORD for the Global Reefers API not set in .env')
    db = dataset.connect(uri)

    # None means "no filter": one query over every season of the report.
    seasons = list(seasons) if seasons else [None]
    commodities = list(commodities) if commodities else list(COMMODITY_SPECIES)
    unknown = [c for c in commodities if c not in COMMODITY_SPECIES]
    if unknown:
        raise ValueError(f'Unknown commodity(ies): {unknown}. '
                         f'Known: {list(COMMODITY_SPECIES)}')
    species = [COMMODITY_SPECIES[c] for c in commodities]

    title = (f'+++ Running Scraper Chile for seasons '
             f'{[s or "all" for s in seasons]} commodities {commodities} '
             f'ETD {date_from or "*"}..{date_to or "*"}')
    logger.info(title)

    if TEST_RESPONSE:
        logger.info('TEST_RESPONSE MODE ON, reading data from json file...')

    records_table_name = os.getenv('RECORDS_TABLE', 'records')
    history_table_name = os.getenv('HISTORY_TABLE', f'{records_table_name}_original')

    # Check if all columns in the table are part of the contract (_columns_set),
    # if any column is unknown, raise an error
    validate_table_columns(db[records_table_name])

    updated_items = []
    new_items = []
    deleted_items = []
    duplicated_items_found = []
    processed_table = None

    total_items_proccesed = 0
    total_requests = 0
    first_request = True
    scopes_seen = []

    for season in seasons:
        for specie in species:
            if not first_request and not TEST_RESPONSE:
                run_sleep(settings.REQUEST_DELAY)
            first_request = False

            logger.info(f'Fetching season={season or "all"} specie={specie or "all"} '
                        f'etd={date_from or "*"}..{date_to or "*"}')
            data = request_report(season, specie, date_from, date_to)
            total_requests += data['requests_made']

            if data['rows'] is None:
                logger.error(f'[{season or "all"}/{specie or "all"}] Read failed, '
                             f'stored rows left untouched.')
                continue
            if not data['rows']:
                logger.info(f'[{season or "all"}/{specie or "all"}] No data found')
                continue

            logger.info(
                f'[{season or "all"}/{specie or "all"}] Reading {len(data["rows"]):,} '
                f'items from {"file" if TEST_RESPONSE else "SERVER"} '
                f'({data["requests_made"]} request(s))'
            )

            for row_season, group in split_items(data['rows']).items():
                # create dataframe for every data group
                original_items = pd.DataFrame(group['original_items'])
                processed_items = pd.DataFrame(group['processed_items'])

                # Cast dataframe for every df
                original_items = cast_dataframe(original_items, column_data_types)
                processed_items = cast_dataframe(processed_items, column_data_types)

                if not settings.ALLOW_DUPLICATES:
                    #! NOT RECOMMENDED: the server may legitimately report two
                    #! rows sharing a key, and dropping them loses data.
                    processed_items = processed_items.drop_duplicates(
                        subset=[*unique_together_keys, *updatable_keys], keep='first')

                #! The scope is what keeps one run from touching rows it did not
                #! read: the season the rows carry, the species asked for (if
                #! any) and the ETD range asked for (if any). Without a date
                #! range the whole season was read, so a shipment missing from it
                #! is really gone and gets deleted.
                filters = {'season': row_season}
                if specie:
                    filters['specie'] = {'in': sorted(set(original_items['specie']))}
                if date_from:
                    filters['etd'] = {'>=': date_from}
                if date_to:
                    filters.setdefault('etd', {})
                    filters['etd'] = {**filters['etd'], '<=': date_to}
                scopes_seen.append(filters)

                with db as transaction:
                    # Accessing tables
                    processed_table = transaction[records_table_name]
                    original_table = transaction[history_table_name]

                    # problem with dtypes: the columns have to exist with the
                    # right type before anything is read back out of them.
                    set_column_types(processed_table, columns_specification)
                    set_column_types(original_table, columns_specification)

                    # Truncate table original data
                    truncate_table(original_table, filters)

                    # register original items from server (before value filtering)
                    original_table.insert_many(original_items.to_dict('records'))

                    # validate incoming items from scrap with existing items in db
                    if truncate:
                        count = truncate_table(processed_table, filters)
                        logger.info(f'[{row_season}] {count} deleted items from '
                                    f'{records_table_name} table.')

                    # Get existing records
                    existing_records = pd.DataFrame(processed_table.find(**filters))

                    duplicated_items = find_duplicated_records(
                        existing_records, processed_items)
                    if not duplicated_items.empty:
                        logger.warning(
                            f'----DELETING DUPLICATED RECORDS BEFORE INSERT '
                            f'(by unique_together) '
                            f'({duplicated_items.shape[0]})-----')
                        logger.warning('Note: These records will be removed from the '
                                       'database and created again as New Items, '
                                       'please check the results.')
                        logger.warning(f'\n{duplicated_items[columns_to_preview]}')
                        duplicated_items_found.append(duplicated_items)
                        processed_table.delete(
                            id=[int(i) for i in duplicated_items['id']])

                        existing_records = pd.DataFrame(
                            processed_table.find(**filters))

                    # It might be empty or not.
                    if existing_records.empty:
                        existing_records = format_empty_dataframe(column_data_types)

                    _new, _updatable, _deletable = diff_records(
                        processed_items, existing_records)

                    new_items.append(_new)
                    updated_items.append(_updatable)

                    total_items_proccesed += len(processed_items)

                    if not _deletable.empty:
                        processed_table.delete(id=[int(i) for i in _deletable['id']])
                        deleted_items.append(_deletable)

    # UPDATE records in DB
    _updated_items = concat(updated_items)
    if not _updated_items.empty:
        processed_table.update_many(
            _updated_items.to_dict('records'), unique_together_keys)

    # INSERT new records in DB
    _new_items = concat(new_items)
    if not _new_items.empty:
        processed_table.insert_many(_new_items.to_dict('records'))

    _deleted_items = concat(deleted_items)
    _duplicated_items = concat(duplicated_items_found)

    # CHECK POST SAVE CHANGES IN DB
    # Counted over the same scopes the run actually touched.
    db_items = 0
    db_items_counted = processed_table is not None and bool(scopes_seen)
    if db_items_counted:
        db_items = sum(processed_table.count(**filters) for filters in scopes_seen)

    summary = RunSummary(
        dataset=DATASET_LABEL,
        seasons=sorted({f['season'] for f in scopes_seen}) or
                [s or 'all' for s in seasons],
        commodities=[f'{c} ({COMMODITY_SPECIES[c]})' for c in commodities],
        etd_range=f'{date_from or "*"} .. {date_to or "*"}',
        total_requests=total_requests,
        total_items=total_items_proccesed,
        new_items=_new_items,
        updated_items=_updated_items,
        deleted_items=_deleted_items,
        duplicated_items=_duplicated_items,
        db_items=db_items if db_items_counted else None,
        duration_seconds=(datetime.now() - run_started).total_seconds(),
    )
    summary_string = summary.as_text()

    if DEBUG:
        logger.info(f'\n{summary_string}')
        # Repeated on its own line so a mismatch lands in the log at ERROR level,
        # worded exactly as the summary words it.
        if summary.mismatch:
            logger.error(f'{summary.db_check_line} | use --truncate to clean DB')
        else:
            logger.info(summary.db_check_line)

    if send_email_notifications:
        send_mail(
            body=summary_string,
            html_body=summary.as_html(),
            email_subject=summary.subject(),
        )
    else:
        logger.info('Email notifications suppressed.')

    logger.info(f'+++++++ SCRAPER CHILE finished for seasons '
                f'{[s or "all" for s in seasons]} +++++++')
    logger.info('\n')

    return {
        'total_items_proccesed': total_items_proccesed,
        'new_items': _new_items,
        'updated_items': _updated_items,
        'deleted_items': _deleted_items,
        'duplicated_items': _duplicated_items,
        'db_items': db_items,
    }


# --- CLI --------------------------------------------------------------------

def parse_args():
    parser = argparse.ArgumentParser(
        description='Chile scraper: Global Reefers DecoFrut FCM shipment report')
    parser.add_argument(
        'seasons',
        nargs='*',
        help='Seasons to scrape, as the report spells them (2025-2026) or by '
             'their closing year (2026). Defaults to the whole report.',
    )
    parser.add_argument(
        '--commodity',
        nargs='+',
        choices=list(COMMODITY_SPECIES),
        metavar='COMMODITY',
        help=f'Commodities to scrape ({", ".join(COMMODITY_SPECIES)}). One '
             f'query per commodity, on the specie the report files it under. '
             f'Defaults to all of them.',
    )
    parser.add_argument(
        '--from',
        dest='date_from',
        type=parse_date,
        metavar='YYYY-MM-DD',
        help='First ETD to scrape (inclusive), sent as fechaDesde.',
    )
    parser.add_argument(
        '--to',
        dest='date_to',
        type=parse_date,
        metavar='YYYY-MM-DD',
        help='Last ETD to scrape (inclusive), sent as fechaHasta.',
    )
    parser.add_argument(
        '--truncate',
        action='store_true',
        help='Delete the stored rows for the scraped scope before comparing, so '
             'everything the server returns is re-inserted as new.',
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Skip the confirmation prompt shown by --truncate.',
    )
    parser.add_argument(
        '--email',
        action='store_true',
        help='Send the run summary by email.',
    )
    return parser.parse_args()


def confirm_truncate(seasons, commodities, date_from, date_to, table_name) -> bool:
    seasons_str = ', '.join(seasons) if seasons else 'all'
    commodities_str = ', '.join(commodities or COMMODITY_SPECIES)
    print(f"\nWARNING: This will delete the stored records for seasons [{seasons_str}] "
          f"commodities [{commodities_str}] ETD [{date_from or '*'}..{date_to or '*'}] "
          f"from table '{table_name}' before re-inserting them.")
    return input("Type 'yes' to confirm: ").strip().lower() == 'yes'


if __name__ == '__main__':
    args = parse_args()

    seasons = [parse_season(s) for s in args.seasons]

    if args.date_from and args.date_to and args.date_from > args.date_to:
        print('--from must not be later than --to')
        sys.exit(1)

    if args.truncate and not args.yes:
        if not confirm_truncate(seasons, args.commodity, args.date_from, args.date_to,
                                os.getenv('RECORDS_TABLE', 'records')):
            print('Aborted.')
            sys.exit(0)

    scrape(
        seasons,
        commodities=args.commodity,
        date_from=args.date_from,
        date_to=args.date_to,
        truncate=args.truncate,
        send_email_notifications=args.email,
    )
