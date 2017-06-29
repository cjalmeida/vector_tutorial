from pathlib import Path
import pandas as pd
import datetime
import numpy as np
from tqdm import tqdm_notebook, tqdm

STATES = ['MA', 'CT', 'NH', 'RI', 'VT', 'ME']
START = datetime.date(2015, 1, 1)
END = datetime.date(2016, 12, 31)
BATCH_SIZE = 2000
CUSTOMERS = 500000

np.random.seed(42)

MEAN_DAILY_CONSUMPTION = 30.
MONTH_CONSUMPTION_ADJ = [1.62, 1.49, 0.90, 0.12, 0.66, 1.25, 1.58, 1.38, 0.92, 0.21, 0.51, 1.36]
MAINS_VOLTAGE = 120
MAINS_VOLTAGE_SD = MAINS_VOLTAGE * 0.1
OUTAGE_PROB = 0.01


def _load_date_dim(conn):
    d = START
    st = 'INSERT INTO date_dim ' \
         '(skey, "date", "day", "month", "year", day_of_week, week, quarter) VALUES ' \
         '(?, ?, ?, ?, ?, ?, ?, ?)'
    batch = []
    keys = []
    cursor = conn.cursor()

    total = (END - START).days
    progress = tqdm_notebook(total=total, desc="Loading date_dim")

    def commit():
        progress.update(len(batch))
        cursor.executemany(st, batch)
        conn.commit()
        batch.clear()

    while d <= END:
        skey = int(d.isoformat().replace('-', ''))
        date = d.isoformat()
        quarter = ((d.month - 1) // 3) + 1
        week = d.isocalendar()[1]
        batch.append((skey, date, d.day, d.month, d.year, d.isoweekday(), week, quarter))
        d += datetime.timedelta(1)
        if len(batch) == BATCH_SIZE:
            commit()
        keys.append(skey)

    if batch:
        commit()

    return keys

@profile
def _load_customers_dim(conn):
    # load zip file and filter by New England states
    zipfile = Path(__file__).parent / 'zipcodes.csv'
    zipcodes = pd.read_csv(str(zipfile))
    zipcodes = zipcodes[zipcodes['state'].isin(STATES)].set_index('zip')
    zips = zipcodes.index.values

    st = 'INSERT INTO customer_dim(skey, zipcode, county, state) VALUES (?, ?, ?, ?)'
    batch = []
    keys = []
    progress = tqdm(total=CUSTOMERS, desc="Loading customer_dim")
    cursor = conn.cursor()

    def commit():
        progress.update(len(batch))
        cursor.executemany(st, batch)
        conn.commit()
        batch.clear()

    n_zips = len(zips)
    for skey in range(CUSTOMERS):
        i = np.random.randint(0, n_zips)
        zip_ = int(zips[i])
        county, state = zipcodes.loc[zip_, ['county', 'state']].values
        batch.append((skey, zip_, county, state))
        if len(batch) == BATCH_SIZE:
            commit()
        keys.append(skey)

    if batch:
        commit()

    return keys


def load_data(conn):
    # Load customers
    customer_keys = _load_customers_dim(conn)

    # Load for the date dimension
    date_keys = _load_date_dim(conn)

    st = 'INSERT INTO ' \
         'metering_fact (customer_skey, date_skey, consumption, min_voltage, max_voltage, outage) values ' \
         '(?, ?, ?, ?, ?, ?)'
    batch = []
    progress = tqdm_notebook(total=len(customer_keys) * len(date_keys), desc="Loading metering_fact")
    cursor = conn.cursor()

    def commit():
        cursor.executemany(st, batch)
        progress.update(len(batch))
        conn.commit()
        batch.clear()

    # Iterate to create measurements
    for date_skey in date_keys:
        month = int(str(date_skey)[4:6])
        monthly_con_mean = MEAN_DAILY_CONSUMPTION * MONTH_CONSUMPTION_ADJ[month - 1]
        monthly_con_sd = monthly_con_mean * 0.40
        for customer_skey in customer_keys:
            outage = 1 if np.random.rand() < OUTAGE_PROB else 0
            consumption = max(0, np.random.normal(monthly_con_mean, monthly_con_sd))
            max_voltage = -1
            while max_voltage < MAINS_VOLTAGE:
                max_voltage = np.random.normal(MAINS_VOLTAGE, MAINS_VOLTAGE_SD)
            min_voltage = MAINS_VOLTAGE * 2
            while min_voltage > MAINS_VOLTAGE:
                min_voltage = np.random.normal(MAINS_VOLTAGE, MAINS_VOLTAGE_SD)

            batch.append((customer_skey, date_skey, int(consumption), int(min_voltage), int(max_voltage), outage))
            if len(batch) == BATCH_SIZE:
                commit()

    if batch:
        commit()


class FileConn():
    def __init__(self, loc):
        self.file = open(loc, 'w')

    def cursor(self):
        file = self.file

        class Cursor():
            def executemany(self, st, data):
                for l in data:
                    file.write(','.join(map(str, l)))
                    file.write('\h')

        return Cursor()

    def commit(self):
        self.file.flush()


if __name__ == '__main__':
    import turbodbc
    import os

    print(os.getenv('ODBCSYSINI'))

    # Setup the connection string to open 'testdb' in a local connection
    # conn_string = "Driver={/opt/Actian/VectorVW/ingres/lib/libiiodbcdriver.1.so};Server=(local);Database=testdb"

    # Connect and set encoding
    # conn = turbodbc.connect('testdb')
    # load_data(conn)

    conn = FileConn('/tmp/customer.csv')
    _load_customers_dim(conn)
