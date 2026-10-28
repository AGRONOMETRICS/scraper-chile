from typing import Dict, Optional
from sqlalchemy import (
    UnicodeText,
    Integer,
    Float,
    Boolean,
)

# Column contract for the DecoFrut FCM shipment report.
#
#   name: (sqlalchemy type, python type, role)
#
# role drives how main.py compares incoming rows against the ones already
# stored:
#   'unique' -> part of the natural key that identifies a row
#   None     -> a measured value; a change here makes the row updatable
#   'label'  -> descriptive text; written but never compared
#   'ignore' -> internal bookkeeping column, not part of the payload comparison
#
# The API's `rowId` is regenerated on every daily load, so it is not stored and
# is not part of the key: a row is identified by everything that describes the
# shipment, and only its volume can move.
_columns_set: Dict[str, Optional[tuple]] = {
    'season':                   (UnicodeText(), str, 'unique'),
    'etd':                      (UnicodeText(), str, 'unique'),
    'etd_week':                 (UnicodeText(), str, 'unique'),
    'market':                   (UnicodeText(), str, 'unique'),
    'country':                  (UnicodeText(), str, 'unique'),
    'importer':                 (UnicodeText(), str, 'unique'),
    'exporter':                 (UnicodeText(), str, 'unique'),
    'pol':                      (UnicodeText(), str, 'unique'),
    'pod':                      (UnicodeText(), str, 'unique'),
    'specie':                   (UnicodeText(), str, 'unique'),
    'variety':                  (UnicodeText(), str, 'unique'),
    'production_method':        (UnicodeText(), str, 'unique'),
    'cargo_type':               (UnicodeText(), str, 'unique'),
    'vessel':                   (UnicodeText(), str, 'unique'),

    # Our name for the specie (Blueberries, Grapes), resolved by main.py from
    # COMMODITIES. Derived from `specie`, so it adds nothing to the key.
    'commodity':                (UnicodeText(), str, 'label'),

    # Groupings derived from the destination. Carried on insert and update but
    # kept out of the key so a regrouping on the Global Reefers side does not
    # read as a brand new shipment.
    'market_group':             (UnicodeText(), str, 'label'),
    'region':                   (UnicodeText(), str, 'label'),

    # Measured values: these are what an update actually updates.
    'boxes':                    (Float(precision=53), float, None),
    'kilograms':                (Float(precision=53), float, None),

    # Extra fields
    'etl_processed':            (Boolean(), bool, 'ignore'),
}

columns_specification = {name: spec[0] for name, spec in _columns_set.items()}

unique_together_keys = [name for name, spec in _columns_set.items() if spec[2] == 'unique']
updatable_keys = [name for name, spec in _columns_set.items() if spec[2] is None]
label_keys = [name for name, spec in _columns_set.items() if spec[2] == 'label']

fields_to_ignore = [name for name, spec in _columns_set.items() if spec[2] == 'ignore']
fields_to_ignore.append('id')

column_data_types = [(name, spec[1]) for name, spec in _columns_set.items()]

# Columns printed in the run summary. Enough of the key to recognise a row,
# plus every value that could have moved.
columns_to_preview = [
    'commodity',
    'season',
    'etd',
    'country',
    'exporter',
    'importer',
    'variety',
    'cargo_type',
] + updatable_keys


def set_column_types(table, types):
    """Create every contract column the table is missing.

    Safe to call on a table that already exists: `create_column` is a no-op for
    a column that is already there, so this doubles as the migration path when
    a column is added to the contract.
    """
    for key, value in types.items():
        table.create_column(key, value)
