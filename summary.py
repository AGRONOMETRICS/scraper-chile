"""Run summary rendering: plain text for the log, HTML for the notification email.

Both views are built from the same `RunSummary`, so what the log shows and what
lands in someone's inbox can never disagree. The text view stays the fallback
part of the message for clients that refuse HTML.
"""

from dataclasses import dataclass, field
from datetime import datetime
from html import escape
from typing import List, Optional

import pandas as pd

from databases.db import columns_to_preview

# Rows printed per section. A run that touches thousands of rows should not mail
# thousands of table rows; the tail is reported as a count instead.
MAX_PREVIEW_ROWS = 25

BRAND = '#0F766E'
INK = '#1F2937'
MUTED = '#6B7280'
BORDER = '#E5E7EB'
CANVAS = '#F3F4F6'

FONT = ("-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,Helvetica,Arial,"
        "sans-serif")
MONO = "'SFMono-Regular',Consolas,'Liberation Mono',Menlo,monospace"

# Human labels for the columns previewed in the tables. Anything not listed
# falls back to its column name with the underscores taken out.
COLUMN_LABELS = {
    'commodity': 'Commodity',
    'season': 'Season',
    'etd': 'ETD',
    'etd_week': 'ETD week',
    'country': 'Country',
    'exporter': 'Exporter',
    'importer': 'Importer',
    'specie': 'Specie',
    'variety': 'Variety',
    'cargo_type': 'Cargo',
    'boxes': 'Boxes',
    'kilograms': 'Kilograms',
}

# Columns rendered right-aligned, i.e. the measured values rather than the key.
NUMERIC_COLUMNS = {'boxes', 'kilograms'}


def _label(column: str) -> str:
    return COLUMN_LABELS.get(column, column.replace('_', ' ').capitalize())


def _preview_columns(dataframe: pd.DataFrame) -> List[str]:
    return [column for column in columns_to_preview if column in dataframe.columns]


def _format_value(value, column: str) -> str:
    """One cell as it should read in the summary.

    Only the measured columns get thousands separators and two decimals; the
    rest of the preview is made of text (dates, names, species), where a
    separator would misread as a quantity.
    """
    if value is None or (not isinstance(value, (list, dict)) and pd.isna(value)):
        return '-'
    if isinstance(value, bool):
        return 'yes' if value else 'no'
    if column in NUMERIC_COLUMNS:
        return f'{float(value):,.2f}'
    if isinstance(value, float):
        return f'{value:g}'
    return str(value)


def _preview_frame(dataframe: pd.DataFrame) -> pd.DataFrame:
    """The first MAX_PREVIEW_ROWS rows, preview columns only, already formatted."""
    columns = _preview_columns(dataframe)
    frame = dataframe[columns].head(MAX_PREVIEW_ROWS).copy()
    for column in columns:
        frame[column] = [_format_value(value, column) for value in frame[column]]
    frame.columns = [_label(column) for column in columns]
    return frame


@dataclass
class Section:
    """One group of rows the run produced, with how the summary should present it."""

    key: str
    title: str
    note: str
    color: str
    items: pd.DataFrame

    @property
    def count(self) -> int:
        return len(self.items)


@dataclass
class RunSummary:
    """Everything one scrape produced, in the shape the summary needs it."""

    seasons: List[str]
    commodities: List[str]
    total_requests: int
    total_items: int
    new_items: pd.DataFrame
    updated_items: pd.DataFrame
    deleted_items: pd.DataFrame
    duplicated_items: pd.DataFrame
    # Which report this run read, e.g. 'reportes/decofrut'.
    dataset: Optional[str] = None
    # The ETD range the run asked for, e.g. '2026-01-01 .. *'.
    etd_range: Optional[str] = None
    db_items: Optional[int] = None
    duration_seconds: Optional[float] = None
    finished_at: datetime = field(default_factory=datetime.now)

    # --- derived ------------------------------------------------------------

    @property
    def sections(self) -> List[Section]:
        return [
            Section('new', 'New items',
                    'Inserted into the database on this run.',
                    '#15803D', self.new_items),
            Section('updated', 'Updated items',
                    'Already stored, but the server reported different values.',
                    '#1D4ED8', self.updated_items),
            Section('deleted', 'Deleted items',
                    'Stored rows the server no longer reports.',
                    '#B91C1C', self.deleted_items),
            Section('duplicated', 'Duplicates cleaned',
                    'Rows the server sent more than once for the same key.',
                    '#B45309', self.duplicated_items),
        ]

    @property
    def mismatch(self) -> Optional[int]:
        """Rows stored minus rows received, or None when the check did not run."""
        if self.db_items is None:
            return None
        return self.db_items - self.total_items

    @property
    def status(self) -> str:
        return 'OK' if not self.mismatch else 'CHECK'

    @property
    def db_check_line(self) -> str:
        """The stored-vs-received comparison, worded the way the log states it."""
        if self.db_items is None:
            return (f'DB items: n/a | Server items: {self.total_items:,} | '
                    'NOT CHECKED')
        verdict = 'OK' if not self.mismatch else f'ITEMS MISMATCH: {self.mismatch:+,}'
        return (f'DB items: {self.db_items:,} | '
                f'Server items: {self.total_items:,} | {verdict}')

    @property
    def duration_text(self) -> str:
        if self.duration_seconds is None:
            return 'n/a'
        minutes, seconds = divmod(int(self.duration_seconds), 60)
        return f'{minutes}m {seconds}s' if minutes else f'{seconds}s'

    @property
    def metrics(self):
        return [
            ('Requests', self.total_requests, MUTED),
            ('Received', self.total_items, INK),
            ('Created', len(self.new_items), '#15803D'),
            ('Updated', len(self.updated_items), '#1D4ED8'),
            ('Deleted', len(self.deleted_items), '#B91C1C'),
            ('Duplicates', len(self.duplicated_items), '#B45309'),
        ]

    @property
    def scope(self):
        """What the run was asked to cover. Shown as-is in both views."""
        rows = []
        if self.dataset:
            rows.append(('Dataset', self.dataset))
        rows += [
            ('Seasons', ', '.join(str(season) for season in self.seasons)),
            ('Commodities', ', '.join(self.commodities)),
        ]
        if self.etd_range:
            rows.append(('ETD range', self.etd_range))
        return rows

    @property
    def run_info(self):
        """When the run ended and how long it took, kept out of the scope block
        because the HTML header already carries it."""
        return [
            ('Finished', self.finished_at.strftime('%Y-%m-%d %H:%M:%S')),
            ('Duration', self.duration_text),
        ]

    def subject(self) -> str:
        parts = [f'{self.total_items:,} received']
        if len(self.new_items):
            parts.append(f'{len(self.new_items):,} new')
        if len(self.updated_items):
            parts.append(f'{len(self.updated_items):,} updated')
        if len(self.deleted_items):
            parts.append(f'{len(self.deleted_items):,} deleted')
        if len(parts) == 1:
            parts.append('no changes')
        flag = '' if self.status == 'OK' else ' [CHECK]'
        return f'Chile Scraper | {" | ".join(parts)}{flag}'

    # --- text ---------------------------------------------------------------

    def as_text(self) -> str:
        width = 78
        lines = [
            '=' * width,
            ' AGRONOMETRICS - CHILE SCRAPER (GLOBAL REEFERS DECOFRUT)',
            '=' * width,
        ]
        details = self.scope + self.run_info
        pad = max(len(name) for name, _ in details)
        for name, value in details:
            lines.append(f'  {name.ljust(pad)} : {value}')

        lines += ['', '-' * width, ' RESULTS', '-' * width]
        for name, value, _ in self.metrics:
            lines.append(f'  {name.ljust(12)} {value:>10,}')
        if self.db_items is not None:
            lines.append(f'  {"In database".ljust(12)} {self.db_items:>10,}')
        lines += ['', f'  {self.db_check_line}']
        if self.mismatch:
            lines.append('  Use --truncate to clean DB and re-insert the scope.')

        for section in self.sections:
            lines += ['', '-' * width,
                      f' {section.title.upper()} ({section.count:,})',
                      '-' * width]
            if section.items.empty:
                lines.append('  No items found')
                continue
            table = _preview_frame(section.items).to_string(index=False)
            lines += [f'  {row}' for row in table.splitlines()]
            if section.count > MAX_PREVIEW_ROWS:
                lines.append(f'  ... and {section.count - MAX_PREVIEW_ROWS:,} more rows')

        lines.append('=' * width)
        return '\n'.join(lines)

    # --- html ---------------------------------------------------------------

    def as_html(self) -> str:
        sections = ''.join(self._html_section(section) for section in self.sections)
        return (
            '<!DOCTYPE html><html><head><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<title>{escape(self.subject())}</title></head>'
            f'<body style="margin:0;padding:0;background:{CANVAS};">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="background:{CANVAS};padding:24px 12px;">'
            '<tr><td align="center">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="max-width:880px;background:#FFFFFF;border:1px solid {BORDER};'
            f'border-radius:10px;overflow:hidden;font-family:{FONT};">'
            f'{self._html_header()}'
            f'{self._html_scope()}'
            f'{self._html_metrics()}'
            f'{self._html_status()}'
            f'{sections}'
            f'{self._html_footer()}'
            '</table></td></tr></table></body></html>'
        )

    def _html_header(self) -> str:
        finished = self.finished_at.strftime('%d %b %Y, %H:%M')
        return (
            f'<tr><td style="background:{BRAND};padding:24px 28px;">'
            '<div style="color:#FFFFFF;font-size:19px;font-weight:600;'
            'letter-spacing:.2px;">Chile Scraper - Global Reefers DecoFrut</div>'
            '<div style="color:#CCFBF1;font-size:13px;padding-top:6px;">'
            f'Run completed {escape(finished)} &middot; '
            f'{escape(self.duration_text)}</div>'
            '</td></tr>'
        )

    def _html_scope(self) -> str:
        rows = ''.join(
            '<tr>'
            f'<td style="padding:4px 16px 4px 0;color:{MUTED};font-size:12px;'
            f'white-space:nowrap;vertical-align:top;">{escape(name)}</td>'
            f'<td style="padding:4px 0;color:{INK};font-size:12px;'
            f'font-family:{MONO};">{escape(value) or "-"}</td>'
            '</tr>'
            for name, value in self.scope
        )
        return (
            f'<tr><td style="padding:20px 28px;border-bottom:1px solid {BORDER};">'
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;font-family:{FONT};">{rows}</table>'
            '</td></tr>'
        )

    def _html_metrics(self) -> str:
        metrics = self.metrics
        cells = ''
        for position, (name, value, color) in enumerate(metrics):
            divider = ('' if position == len(metrics) - 1
                       else f'border-right:1px solid {BORDER};')
            cells += (
                f'<td align="center" style="padding:16px 6px;{divider}">'
                f'<div style="font-size:22px;font-weight:600;color:{color};">'
                f'{value:,}</div>'
                f'<div style="font-size:11px;color:{MUTED};text-transform:uppercase;'
                f'letter-spacing:.6px;padding-top:4px;">{escape(name)}</div></td>'
            )
        return (
            f'<tr><td style="padding:0;border-bottom:1px solid {BORDER};">'
            '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
            f'style="font-family:{FONT};table-layout:fixed;"><tr>{cells}</tr></table>'
            '</td></tr>'
        )

    def _html_status(self) -> str:
        """The stored-vs-received check, always shown, verdict included."""
        if self.db_items is None:
            background, color, border = '#F9FAFB', '#4B5563', BORDER
            note = ('The stored row count was not compared: this run read no '
                    'season, so there is no scope to count.')
        elif not self.mismatch:
            background, color, border = '#ECFDF5', '#065F46', '#A7F3D0'
            note = ('The rows stored for the scraped scope match what the server '
                    'returned.')
        else:
            background, color, border = '#FEF2F2', '#991B1B', '#FECACA'
            note = ('Stored and received row counts differ. Re-run with '
                    '--truncate to rebuild the scope.')
        return (
            f'<tr><td style="padding:16px 28px;border-bottom:1px solid {BORDER};">'
            f'<div style="background:{background};border:1px solid {border};'
            'border-radius:6px;padding:12px 14px;">'
            f'<div style="font-family:{MONO};font-size:13px;font-weight:600;'
            f'color:{color};">{escape(self.db_check_line)}</div>'
            f'<div style="font-size:12px;color:{color};opacity:.85;'
            f'padding-top:5px;line-height:1.5;">{escape(note)}</div>'
            '</div></td></tr>'
        )

    def _html_section(self, section: Section) -> str:
        heading = (
            f'<div style="font-size:14px;font-weight:600;color:{INK};">'
            '<span style="display:inline-block;width:8px;height:8px;'
            f'border-radius:8px;background:{section.color};margin-right:8px;">'
            '</span>'
            f'{escape(section.title)} '
            f'<span style="color:{MUTED};font-weight:500;">({section.count:,})</span>'
            '</div>'
            f'<div style="font-size:12px;color:{MUTED};padding:4px 0 12px 16px;">'
            f'{escape(section.note)}</div>'
        )
        return (
            f'<tr><td style="padding:20px 28px;border-bottom:1px solid {BORDER};">'
            f'{heading}{self._html_table(section)}</td></tr>'
        )

    def _html_table(self, section: Section) -> str:
        if section.items.empty:
            return (f'<div style="font-size:12px;color:{MUTED};font-style:italic;'
                    'padding-left:16px;">No items found</div>')

        columns = _preview_columns(section.items)
        header = ''.join(
            f'<th align="{"right" if column in NUMERIC_COLUMNS else "left"}" '
            f'style="padding:7px 10px;font-size:10px;font-weight:600;color:{MUTED};'
            'text-transform:uppercase;letter-spacing:.5px;white-space:nowrap;'
            f'border-bottom:1px solid {BORDER};">{escape(_label(column))}</th>'
            for column in columns
        )

        body = ''
        rows = section.items[columns].head(MAX_PREVIEW_ROWS)
        for position, (_, row) in enumerate(rows.iterrows()):
            background = '#FFFFFF' if position % 2 else '#FAFAFA'
            cells = ''.join(
                f'<td align="{"right" if column in NUMERIC_COLUMNS else "left"}" '
                f'style="padding:7px 10px;font-size:12px;color:{INK};'
                f'font-family:{MONO};white-space:nowrap;'
                f'border-bottom:1px solid {BORDER};">'
                f'{escape(_format_value(row[column], column))}</td>'
                for column in columns
            )
            body += f'<tr style="background:{background};">{cells}</tr>'

        remaining = ''
        if section.count > MAX_PREVIEW_ROWS:
            remaining = (
                f'<div style="font-size:11px;color:{MUTED};padding:8px 0 0 2px;">'
                f'Showing the first {MAX_PREVIEW_ROWS} of {section.count:,} rows - '
                f'{section.count - MAX_PREVIEW_ROWS:,} more in the log.</div>'
            )

        return (
            '<div style="overflow-x:auto;">'
            '<table role="presentation" cellpadding="0" cellspacing="0" '
            f'style="width:100%;border-collapse:collapse;border:1px solid {BORDER};">'
            f'<thead><tr style="background:#F9FAFB;">{header}</tr></thead>'
            f'<tbody>{body}</tbody></table></div>{remaining}'
        )

    def _html_footer(self) -> str:
        return (
            '<tr><td style="padding:18px 28px;background:#FAFAFA;">'
            f'<div style="font-size:11px;color:{MUTED};line-height:1.6;">'
            'Automated message from the Agronometrics Chile scraper. '
            'Source: Global Reefers API, DecoFrut FCM shipment report.'
            '</div></td></tr>'
        )
