from pathlib import Path
import pandas as pd
import datetime
import numpy as np
from tqdm import tqdm_notebook as tqdm
# from tqdm import tqdm
from subprocess import run, DEVNULL
import mmap

STATES = ['MA', 'CT', 'NH', 'RI', 'VT', 'ME']
START = datetime.date(2015, 1, 1)
END = datetime.date(2016, 12, 31)
CUSTOMERS = 500000

np.random.seed(42)

MEAN_DAILY_CONSUMPTION = 30.
MONTH_CONSUMPTION_ADJ = [1.62, 1.49, 0.90, 0.12, 0.66, 1.25, 1.58, 1.38, 0.92, 0.21, 0.51, 1.36]
MAINS_VOLTAGE = 120
MAINS_VOLTAGE_SD = MAINS_VOLTAGE * 0.05
OUTAGE_PROB = 0.01


def _load_date_dim(conn):
    d = START
    st = 'INSERT INTO date_dim ' \
         '(skey, "date", "day", "month", "year", day_of_week, week, quarter) VALUES ' \
         '(?, ?, ?, ?, ?, ?, ?, ?)'
    batch_size = 1000
    batch = []
    keys = []
    cursor = conn.cursor()

    def commit():
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
        if len(batch) == batch_size:
            commit()
        keys.append(skey)

    if batch:
        commit()

    return keys


def _load_customers_dim(conn):
    # load zip file and filter by New England states
    zipfile = Path(__file__).parent / 'zipcodes.csv'
    zipcodes = pd.read_csv(str(zipfile))
    zipcodes = zipcodes[zipcodes['state'].isin(STATES)].values

    st = 'INSERT INTO customer_dim(skey, zipcode, county, state) VALUES (?, ?, ?, ?)'
    batch_size = 1000
    batch = []
    keys = []
    progress = tqdm(total=CUSTOMERS, desc="Loading customer_dim")
    cursor = conn.cursor()

    def commit():
        progress.update(len(batch))
        cursor.executemany(st, batch)
        conn.commit()
        batch.clear()

    n_zips = len(zipcodes)
    for skey in range(CUSTOMERS):
        i = np.random.randint(0, n_zips)
        zip_, county, state = zipcodes[i]
        batch.append((skey, zip_, county, state))
        if len(batch) == batch_size:
            commit()
        keys.append(skey)

    if batch:
        commit()

    return keys


def upload_with_mmap(batch):
    file_len = len(batch) * 6 * 64
    fname = '/tmp/vector_mmap_file.csv'
    with open(fname, 'w') as f:
        f.seek(file_len)
        f.truncate()
    with open(fname, 'r+') as f:
        m = mmap.mmap(f.fileno(), 0)
        data = [','.join(map(str, row)) for row in batch]
        data = '\n'.join(data)
        m.write(bytes(data, 'ascii'))
        m.write(b'\n')
        l = m.tell()
        m.close()
        f.truncate(l)

    st = r"COPY TABLE metering_fact " \
         "(customer_skey=c0, date_skey=c0, consumption=c0, min_voltage=c0, max_voltage=c0, outage=c0) " \
         "FROM '%s'\g" % fname

    # We're calling the "sql" command because Ingres ODBC Driver does not currently support COPY command
    p = run(['sql', 'testdb'], input=bytes(st, 'ascii'), stdout=DEVNULL)
    assert p.returncode == 0


def load_data(conn):
    # Load customers
    customer_keys = _load_customers_dim(conn)

    # Load for the date dimension
    date_keys = _load_date_dim(conn)

    N = len(customer_keys) * len(date_keys)
    batch_size = 100000
    batch = np.zeros((batch_size, 6), dtype=np.int32)
    progress = tqdm(total=N, desc="Loading metering_fact")

    def commit():
        upload_with_mmap(batch.tolist())
        progress.update(len(batch))

    for date_skey in date_keys:
        month = int(str(date_skey)[4:6])
        con_mean = MEAN_DAILY_CONSUMPTION * MONTH_CONSUMPTION_ADJ[month - 1]
        con_sd = con_mean * 0.40
        b = 0
        while b < CUSTOMERS:
            end = min(CUSTOMERS, b + batch_size)
            batch[:, 0] = customer_keys[b:end]
            batch[:, 1] = date_skey

            # consumption
            x = np.random.normal(con_mean, con_sd, size=batch_size)
            x[x < 0] = 0
            batch[:, 2] = x

            # max_voltage
            x = MAINS_VOLTAGE + np.abs(np.random.normal(0, MAINS_VOLTAGE_SD, size=batch_size))
            batch[:, 3] = x

            # min_voltage
            x = MAINS_VOLTAGE - np.abs(np.random.normal(0, MAINS_VOLTAGE_SD, size=batch_size))
            batch[:, 4] = x

            # outage
            x = (np.random.random(size=batch_size) < OUTAGE_PROB).astype(np.uint8)
            batch[:, 5] = x

            commit()
            b = end


if __name__ == '__main__':
    import turbodbc
    import os

    print(os.getenv('ODBCSYSINI'))

    # Setup the connection string to open 'testdb' in a local connection
    # conn_string = "Driver={/opt/Actian/VectorVW/ingres/lib/libiiodbcdriver.1.so};Server=(local);Database=testdb"

    # Connect and set encoding
    conn = turbodbc.connect('testdb')
    # _load_customers_dim(conn)
    load_data(conn)
