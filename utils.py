import argparse
import os
import re
import sys
import time
from datetime import date
from functools import wraps
from typing import List, Tuple

import mailchimp_transactional as MailchimpTransactional
import pandas as pd

from config import get_logger, settings

logger = get_logger(name='scraper_chile', log_filename='scraper_chile.log')


def run_sleep(n: int):
    logger.info(f'Waiting for {n} seconds...')
    time.sleep(n)


def truncate_table(table, filters):
    """Delete every row matching `filters` and report how many there were."""
    count_before = table.count(**filters)
    table.delete(**filters)
    return count_before


def format_empty_dataframe(columns: List[Tuple[str, type]]) -> pd.DataFrame:
    """
    Creates an empty DataFrame with the specified columns.

    Args:
        columns (List[Tuple[str, type]]): A list of tuples where each tuple contains the column name
            and the data type of the column.

    Returns:
        pd.DataFrame: An empty DataFrame with the specified columns.

    Example:
        >>> columns = [('A', int), ('B', float), ('C', str)]
        >>> df = format_empty_dataframe(columns)
        >>> print(df)
        Empty DataFrame
        Columns: [A, B, C]
        Index: []
    """
    df = pd.DataFrame({name: pd.Series(dtype=dtype) for name, dtype in columns})
    column_names = [name for name, _ in columns]
    return df[column_names]


def cast_dataframe(dataframe: pd.DataFrame, column_data_types: List[Tuple[str, type]]):
    """
    Casts the columns of a DataFrame to specified data types.

    Args:
        dataframe (pd.DataFrame): The DataFrame to cast.
        column_data_types (List[Tuple[str, type]]): A list of tuples specifying the column name
            and its desired data type.

    Returns:
        pd.DataFrame: The DataFrame with columns casted to specified data types.

    Notes:
        If the input DataFrame is empty, it will be formatted according to the specified
        column data types. Columns missing from the DataFrame are left untouched, and a cast
        that cannot be applied is skipped rather than raised.
    """
    # It might be empty or not.
    if dataframe.empty:
        return format_empty_dataframe(column_data_types)

    dataframe.columns.name = None

    for column_name, data_type in column_data_types:
        if column_name not in dataframe.columns:
            continue
        dataframe[column_name] = dataframe[column_name].astype(data_type, errors='ignore')

    return dataframe


def align_dataframes(incoming: pd.DataFrame, existing: pd.DataFrame, keys: List[str]):
    """Force the join columns of both frames to a single dtype.

    `dataset` hands back whatever the DB driver decided (ints as int64, text as
    object) while the incoming frame is built from JSON. A merge across a
    mismatched pair of dtypes silently matches nothing, which would report every
    row as new, so both sides are cast to the same thing before comparing.
    """
    for key in keys:
        if key not in incoming.columns or key not in existing.columns:
            continue
        if incoming[key].dtype != existing[key].dtype:
            incoming[key] = incoming[key].astype(str)
            existing[key] = existing[key].astype(str)
    return incoming, existing


def measure_time(view_func):
    @wraps(view_func)
    def wrapper(*args, **kwargs):
        start_time = time.time()
        result = view_func(*args, **kwargs)
        end_time = time.time()
        logger.info(f"Function '{view_func.__name__}' took {end_time - start_time:.2f} seconds.")
        return result
    return wrapper


def send_mail(body, email_subject='Scraper Chile error alert', html_body=None):
    """Send a notification.

    `body` is the plain-text part and is always sent; pass `html_body` to add the
    formatted version on top of it, which is what clients render when they can.
    """
    api_key = settings.MAILCHIMP_API_KEY
    recipients = list(filter(None, (os.getenv("TO_EMAIL") or '').split(",")))

    if not api_key:
        logger.warning('MAILCHIMP_API_KEY is not set, skipping email notification.')
        return
    if not recipients:
        logger.warning('TO_EMAIL is not set, skipping email notification.')
        return

    mailchimp = MailchimpTransactional.Client(api_key)

    message = {
        "from_email": settings.FROM_EMAIL,
        "from_name": settings.FROM_NAME,
        "subject": email_subject,
        "text": body,
        "to": [{"email": recipient.strip(), "type": "to"} for recipient in recipients],
    }
    if html_body:
        message["html"] = html_body

    try:
        response = mailchimp.messages.send({"message": message})
        logger.info(f"Email successfully sent to {len(response)} recipients.")
    except Exception as e:
        logger.error(f'Could not send the notification email: {e}')


def parse_season(value):
    """Validate a season given on the command line.

    The report spells seasons as '2025-2026'. A bare closing year (2026) is
    accepted as shorthand for the season that ends in it.
    """
    value = str(value).strip()
    match = re.fullmatch(r'(\d{4})-(\d{4})', value)
    if match:
        start, end = int(match.group(1)), int(match.group(2))
    elif re.fullmatch(r'\d{4}', value):
        end = int(value)
        start = end - 1
    else:
        print(f'"{value}" is not a season, use 2025-2026 or 2026')
        sys.exit(1)

    if end != start + 1:
        print(f'Season {value} must span two consecutive years (e.g. 2025-2026)')
        sys.exit(1)
    if not 2000 <= end <= date.today().year + 2:
        print(f'Season {value} is out of the supported range')
        sys.exit(1)

    return f'{start}-{end}'


def parse_date(value):
    """argparse type for an ETD bound, kept as the YYYY-MM-DD text the API takes."""
    try:
        return date.fromisoformat(value).isoformat()
    except ValueError:
        raise argparse.ArgumentTypeError(f'"{value}" is not a YYYY-MM-DD date')
