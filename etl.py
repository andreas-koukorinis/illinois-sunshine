from sunshine.models import Committee, Candidate, Officer, Candidacy, \
    D2Report, FiledDoc, Receipt, Expenditure, Investment
import ftplib
import zipfile
from io import BytesIO
import os
from boto.s3.connection import S3Connection
from boto.s3.key import Key
from datetime import date, datetime
from hashlib import md5
import sqlalchemy as sa
import csv
from csvkit.cleanup import RowChecker
from collections import OrderedDict

class SunshineExtract(object):
    
    def __init__(self, 
                 download_path='downloads',
                 ftp_host=None,
                 ftp_path=None,
                 ftp_user=None,
                 ftp_pw=None,
                 aws_key=None,
                 aws_secret=None):
        
        self.ftp_host = ftp_host
        self.ftp_user = ftp_user
        self.ftp_pw = ftp_pw
        self.ftp_path = ftp_path

        self.aws_key = aws_key
        self.aws_secret = aws_secret
        
        self.bucket_name = 'il-elections'
        self.download_path = download_path
    
    def downloadRaw(self):
        fpaths = []
        with ftplib.FTP(self.ftp_host) as ftp:
            ftp.login(self.ftp_user, self.ftp_pw)
            files = ftp.nlst(self.ftp_path)
            for f in files:
                print('downloading %s' % f)
                fname, fext = f.rsplit('.', 1)
                
                remote_path ='%s/%s' % (self.ftp_path, f)
                local_path = '%s/%s' % (self.download_path, f)

                with open(local_path, 'wb') as fobj:
                    ftp.retrbinary('RETR %s' % remote_path, fobj.write)
                
                fpaths.append(local_path)
        
        return fpaths

    def cacheOnS3(self, fpath):
        
        fname, fext = fpath.rsplit('/', 1)[1].rsplit('.', 1)
        
        print('caching %s.%s' % (fname, fext))

        conn = S3Connection(self.aws_key, self.aws_secret)
        bucket = conn.get_bucket(self.bucket_name)
        
        k = Key(bucket)
        keyname = 'sunshine/%s_%s.%s' % (fname, 
                                         date.today().isoformat(), 
                                         fext)
        k.key = keyname
        
        with open(fpath, 'rb') as fobj:
            k.set_contents_from_file(fobj)
        
        k.make_public()
        
        bucket.copy_key('sunshine/%s_latest.%s' % (fname, fext), 
                        self.bucket_name,
                        keyname,
                        preserve_acl=True)
    
    def download(self, cache=True):
        fpaths = self.downloadRaw()
        
        if cache:
            for path in fpaths:
                self.cacheOnS3(path)
            
            self.zipper()

    def zipper(self):
        outp = BytesIO()
        now = datetime.now().strftime('%Y-%m-%d')
        zf_name = 'IL_Elections_%s' % now
        with zipfile.ZipFile(outp, mode='w') as zf:
            for f in os.listdir(self.download_path):
                if f.endswith('.txt'):
                    zf.write(os.path.join(self.download_path, f), 
                             '%s/%s' % (zf_name, f),
                             compress_type=zipfile.ZIP_DEFLATED)
        
        conn = S3Connection(self.aws_key, self.aws_secret)
        bucket = conn.get_bucket(self.bucket_name)
        k = Key(bucket)
        k.key = '%s.zip' % zf_name
        outp.seek(0)
        k.set_contents_from_file(outp)
        k.make_public()
        bucket.copy_key(
            'IL_Elections_latest.zip', 
            'il-elections', 
            '%s.zip' % zf_name,
            preserve_acl=True)
        
        del outp

class SunshineTransformLoad(object):

    def __init__(self, 
                 engine,
                 metadata=None,
                 chunk_size=50000):

        
        self.engine = engine

        self.chunk_size = chunk_size

        if metadata:
            self.metadata = metadata
            self.initializeDB()
        
        self.file_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 
                                      'downloads', 
                                      self.filename)

    def initializeDB(self):
        enum = ''' 
            CREATE TYPE committee_position AS ENUM (
              'support', 
              'oppose'
            )
        '''
        conn = self.engine.connect()
        trans = conn.begin()
        
        try:
            conn.execute(enum)
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
        
        self.metadata.create_all(bind=self.engine)
    
    def createTempTable(self):
        create = ''' 
            CREATE TABLE temp_{0} AS
              SELECT * FROM {0} LIMIT 1
            WITH NO DATA
        '''.format(self.table_name)
        with self.engine.begin() as conn:
            conn.execute('DROP TABLE IF EXISTS temp_{0}'.format(self.table_name))
            conn.execute(create)
    
    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        return ''' 
            WITH data_update AS (
              UPDATE {0} SET 
                {1}
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE {0}.id = subq.id
            )
            INSERT INTO {0} 
              SELECT temp.* FROM temp_{0} AS temp
              LEFT JOIN {0} AS data
                USING(id)
              WHERE data.id IS NULL
            RETURNING *
        '''.format(self.table_name, 
                   ','.join(update_fields))

    def update(self):

        with self.engine.begin() as conn:
            inserted = list(conn.execute(sa.text(self.upsert)))
            print('inserted %s %s' % (len(inserted), self.table_name))

        # with self.engine.begin() as conn:
        #     conn.execute('DROP TABLE temp_{0}'.format(self.table_name))

    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t', 
                                quoting=csv.QUOTE_NONE)
            checker = RowChecker(reader)
            for row in checker.checked_rows():
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not row[idx]:
                            row[idx] = None
                    yield OrderedDict(zip(self.header, row))

    def load(self):
        self.createTempTable()
        
        insert = ''' 
            INSERT INTO temp_{0} ({1}) VALUES ({2})
        '''.format(self.table_name,
                   ','.join(self.header),
                   ','.join([':%s' % h for h in self.header]))

        rows = []
        i = 1
        for row in self.transform():
            rows.append(row)
            if len(rows) % self.chunk_size is 0:
                
                with self.engine.begin() as conn:
                    conn.execute(sa.text(insert), *rows)
                
                print('Loaded %s %s' % ((i * self.chunk_size), self.table_name))
                i += 1
                rows = []
        if rows:
            with self.engine.begin() as conn:
                conn.execute(sa.text(insert), *rows)
    
class SunshineCommittees(SunshineTransformLoad):
    
    table_name = 'committees'
    header = Committee.__table__.columns.keys()
    filename = 'Committees.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None

                    # Replace status value
                    if row[14] != 'A':
                        row[14] = False
                    else:
                        row[14] = True

                    # Replace position values
                    for idx in [23, 24]:
                        if row[idx] == 'O':
                            row[idx] = 'oppose'
                        elif row[idx] == 'S':
                            row[idx] = 'support'
                        else:
                            row[idx] = None
                    
                    yield OrderedDict(zip(self.header, row))
    

class SunshineCandidates(SunshineTransformLoad):
    
    table_name = 'candidates'
    header = [f for f in Candidate.__table__.columns.keys() \
              if f not in ['date_added', 'last_update', 'ocd_id']]
    filename = 'Candidates.txt'
    
    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        return ''' 
            WITH upsert AS (
              UPDATE {0} SET 
                {1},
                last_update = NOW()
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE {0}.id = subq.id
            )
            INSERT INTO {0} ({2})
              SELECT 
                {3},
                NOW() AS last_update,
                NOW() AS date_added
              FROM temp_{0} AS temp
              LEFT JOIN {0} AS data
                USING(id)
              WHERE data.id IS NULL
            RETURNING *
        '''.format(self.table_name, 
                   ','.join(update_fields),
                   ','.join(self.header + ['last_update', 'date_added']),
                   ','.join(['temp.{0}'.format(f) for f in self.header]))

class SunshineOfficers(SunshineTransformLoad):
    table_name = 'officers'
    header = Officer.__table__.columns.keys()
    filename = 'Officers.txt'
    current = True

    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        
                        if not cell:
                            row[idx] = None
                    
                    # Add empty committee_id
                    row.insert(1, None)

                    # Add empty resign date
                    row.insert(11, None)

                    # Add current flag
                    row.append(self.current)
                    
                    yield OrderedDict(zip(self.header, row))

    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        return ''' 
            WITH upsert AS (
              UPDATE {0} SET 
                {1}
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE officers.id = subq.id
                AND officers.current = subq.current
            )
            INSERT INTO {0} ({2})
              SELECT 
                {3}
              FROM temp_{0} AS temp
              LEFT JOIN {0} AS data
                ON temp.id = data.id 
                AND temp.current = data.current
              WHERE data.id IS NULL
                AND data.current IS NULL
            RETURNING *
        '''.format(self.table_name, 
                   ','.join(update_fields),
                   ','.join(self.header),
                   ','.join(['temp.{0}'.format(f) for f in self.header]))
    
class SunshinePrevOfficers(SunshineOfficers):
    table_name = 'officers'
    header = Officer.__table__.columns.keys()
    filename = 'PrevOfficers.txt'
    current = False
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None
                    
                    # Add empty phone
                    row.insert(10, None)

                    # Add current flag
                    row.append(self.current)

                    yield OrderedDict(zip(self.header, row))

class SunshineCandidacy(SunshineTransformLoad):
    table_name = 'candidacies'
    header = Candidacy.__table__.columns.keys()
    filename = 'CanElections.txt'
    
    election_types = {
        'CE': 'Consolidated Election',
        'GP': 'General Primary',
        'GE': 'General Election',
        'CP': 'Consolidated Primary',
        'NE': None,
        'SE': 'Special Election'
    }

    race_types = {
        'Inc': 'incumbent',
        'Open': 'open seat',
        'Chal': 'challenger',
        'Ret': 'retired',
    }

    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None

                    # Get election type
                    row[2] = self.election_types.get(row[2])
                    
                    # Get race type
                    row[4] = self.race_types.get(row[4])
                    
                    # Get outcome
                    if row[5] == 'Won':
                        row[5] = 'won'
                    elif row[5] == 'Lost':
                        row[5] = 'lost'
                    else:
                        row[5] = None

                    yield OrderedDict(zip(self.header, row))


class SunshineCandidateCommittees(SunshineTransformLoad):
    table_name = 'candidate_committees'
    header = ['committee_id', 'candidate_id']
    filename = 'CmteCandidateLinks.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None
                    row.pop(0)
                    yield OrderedDict(zip(self.header, row))

    @property
    def upsert(self):
        field_format = '{1} = subq.{1}'
        
        update_fields = [field_format.format(self.table_name,f) \
                             for f in self.header]
        
        where_clause = ''' 
            WHERE {0}.{1} = subq.{1}
              AND {0}.{2} = subq.{2}
        '''.format(self.table_name, 
                   self.header[0], 
                   self.header[1])

        return ''' 
            WITH upsert AS (
              UPDATE {0} SET 
                {1}
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              {2}
              RETURNING *
            )
            INSERT INTO {0} 
              SELECT temp.* 
              FROM temp_{0} AS temp
              LEFT JOIN {0} AS data
                ON temp.candidate_id = data.candidate_id
                AND temp.committee_id = data.committee_id
              WHERE data.candidate_id IS NULL
                AND data.committee_id IS NULL
            RETURNING *
        '''.format(self.table_name, 
                   ','.join(update_fields),
                   where_clause)

class SunshineOfficerCommittees(SunshineTransformLoad):
    table_name = 'officers'
    header = ['committee_id', 'officer_id']
    filename = 'CmteOfficerLinks.txt'
    
    def transform(self):
        with open(self.file_path, 'r', encoding='latin1') as f:
            reader = csv.reader(f, delimiter='\t')
            header = next(reader)
            for row in reader:
                if row:
                    for idx, cell in enumerate(row):
                        row[idx] = cell.strip()
                        if not cell:
                            row[idx] = None
                    row.pop(0)
                    yield OrderedDict(zip(self.header, row))

    def createTempTable(self):
        create = ''' 
            CREATE TABLE temp_{0} (
              committee_id INTEGER, 
              officer_id INTEGER
            )
        '''.format(self.table_name)
        with self.engine.begin() as conn:
            conn.execute('DROP TABLE IF EXISTS temp_{0}'.format(self.table_name))
            conn.execute(create)
    
    @property
    def upsert(self):

        return ''' 
              UPDATE officers SET 
                committee_id = subq.committee_id
              FROM (
                SELECT * FROM temp_{0}
              ) AS subq
              WHERE officers.id = subq.officer_id
                AND officers.current = TRUE
              RETURNING *
        '''.format(self.table_name)

class SunshineD2Reports(SunshineTransformLoad):
    table_name = 'd2_reports'
    header = D2Report.__table__.columns.keys()
    filename = 'D2Totals.txt'

class SunshineFiledDocs(SunshineTransformLoad):
    table_name = 'filed_docs'
    header = FiledDoc.__table__.columns.keys()
    filename = 'FiledDocs.txt'

class SunshineReceipts(SunshineTransformLoad):
    table_name = 'receipts'
    header = Receipt.__table__.columns.keys()
    filename = 'Receipts.txt'
    
class SunshineExpenditures(SunshineTransformLoad):
    table_name = 'expenditures'
    header = Expenditure.__table__.columns.keys()
    filename = 'Expenditures.txt'

class SunshineInvestments(SunshineTransformLoad):
    table_name = 'investments'
    header = Investment.__table__.columns.keys()
    filename = 'Investments.txt'

class SunshineViews(object):
    
    def __init__(self, engine):
        self.engine = engine

    def dropViews(self):
        with self.engine.begin() as conn:
            conn.execute('DROP MATERIALIZED VIEW IF EXISTS receipts_by_week')
        with self.engine.begin() as conn:
            conn.execute('DROP MATERIALIZED VIEW IF EXISTS committee_receipts_by_week')
        with self.engine.begin() as conn:
            conn.execute('DROP MATERIALIZED VIEW IF EXISTS incumbent_candidates')
        with self.engine.begin() as conn:
            conn.execute('DROP MATERIALIZED VIEW IF EXISTS most_recent_filings CASCADE')
        with self.engine.begin() as conn:
            conn.execute('DROP MATERIALIZED VIEW IF EXISTS full_search')
        with self.engine.begin() as conn:
            conn.execute('DROP MATERIALIZED VIEW IF EXISTS expenditures_by_candidate')

    def makeAllViews(self):
        self.expendituresByCandidate()
        self.receiptsAggregates()
        self.committeeReceiptAggregates()
        self.incumbentCandidates()
        self.mostRecentFilings()
        self.condensedReceipts()
        self.condensedExpenditures()
        self.committeeMoney()
        self.candidateMoney()
        self.fullSearchView()
    
    def condensedExpenditures(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW condensed_expenditures')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            rec = ''' 
                CREATE MATERIALIZED VIEW condensed_expenditures AS (
                  (
                    SELECT 
                      e.*
                    FROM expenditures AS e
                    JOIN most_recent_filings AS m
                      USING(committee_id)
                    WHERE e.expended_date > m.reporting_period_end
                  ) UNION (
                    SELECT
                      e.*
                    FROM expenditures AS e
                    JOIN (
                      SELECT DISTINCT ON (
                        reporting_period_begin, 
                        reporting_period_end, 
                        committee_id
                      )
                        id AS filed_doc_id
                      FROM filed_docs
                      ORDER BY reporting_period_begin,
                               reporting_period_end,
                               committee_id,
                               received_datetime DESC
                    ) AS f
                      USING(filed_doc_id)
                  )
                )
            '''
            conn.execute(sa.text(rec))
            trans.commit()

    def condensedReceipts(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW condensed_receipts')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            rec = ''' 
                CREATE MATERIALIZED VIEW condensed_receipts AS (
                  (
                    SELECT 
                      r.*
                    FROM receipts AS r
                    JOIN most_recent_filings AS m
                      USING(committee_id)
                    WHERE r.received_date > m.reporting_period_end
                  ) UNION (
                    SELECT
                      r.*
                    FROM receipts AS r
                    JOIN (
                      SELECT DISTINCT ON (
                        reporting_period_begin, 
                        reporting_period_end, 
                        committee_id
                      )
                        id AS filed_doc_id
                      FROM filed_docs
                      ORDER BY reporting_period_begin,
                               reporting_period_end,
                               committee_id,
                               received_datetime DESC
                    ) AS f
                      USING(filed_doc_id)
                  )
                )
            '''
            conn.execute(sa.text(rec))
            trans.commit()

    def expendituresByCandidate(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW expenditures_by_candidate')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            exp = ''' 
                CREATE MATERIALIZED VIEW expenditures_by_candidate AS (
                  SELECT
                    c.id AS candidate_id,
                    MAX(c.first_name) AS first_name,
                    MAX(c.last_name) AS last_name,
                    MAX(c.office) AS office,
                    cm.id AS committee_id,
                    MAX(cm.name) AS committee_name,
                    MAX(cm.type) AS committee_type,
                    bool_or(e.supporting) AS supporting,
                    bool_or(e.opposing) AS opposing,
                    SUM(e.amount) AS total_amount,
                    MIN(e.expended_date) AS min_date,
                    MAX(e.expended_date) AS max_date
                  FROM candidates AS c
                  JOIN expenditures AS e
                    ON c.first_name || ' ' || c.last_name = e.candidate_name
                  JOIN committees AS cm
                    ON e.committee_id = cm.id
                  GROUP BY cm.id, c.id
                )
            '''
            conn.execute(sa.text(exp))
            trans.commit()

    def receiptsAggregates(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW receipts_by_week')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            weeks = ''' 
                CREATE MATERIALIZED VIEW receipts_by_week AS (
                  SELECT 
                    date_trunc('week', received_date) AS week,
                    SUM(amount) AS total_amount,
                    COUNT(id) AS donation_count,
                    AVG(amount) AS average_donation
                  FROM receipts
                  GROUP BY date_trunc('week', received_date)
                  ORDER BY week
                )
            '''
            conn.execute(sa.text(weeks))
            trans.commit()

    def committeeReceiptAggregates(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW committee_receipts_by_week')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            weeks = ''' 
                CREATE MATERIALIZED VIEW committee_receipts_by_week AS (
                  SELECT 
                    committee_id,
                    date_trunc('week', received_date) AS week,
                    SUM(amount) AS total_amount,
                    COUNT(id) AS donation_count,
                    AVG(amount) AS average_donation
                  FROM receipts
                  GROUP BY committee_id,
                           date_trunc('week', received_date)
                  ORDER BY week
                )
            
            '''
            conn.execute(sa.text(weeks))
            trans.commit()

    def incumbentCandidates(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW incumbent_candidates')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            incumbents = '''
                CREATE MATERIALIZED VIEW incumbent_candidates AS (
                  SELECT DISTINCT ON (cd.district, cd.office)
                    cd.*,
                    cs.election_year AS last_election_year,
                    cs.election_type AS last_election_type,
                    cs.race_type AS last_race_type
                  FROM candidates AS cd
                  JOIN candidacies AS cs
                    ON cd.id = cs.candidate_id
                  WHERE cs.outcome = :outcome
                    AND cs.election_year >= :year
                  ORDER BY cd.district, cd.office, cs.id DESC
                )
            '''
            
            conn.execute(sa.text(incumbents), 
                         outcome='won',
                         year=2014)

            trans.commit()

    def mostRecentFilings(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW most_recent_filings')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            create = '''
               CREATE MATERIALIZED VIEW most_recent_filings AS (
                 SELECT 
                   d2.end_funds_available, 
                   d2.total_investments,
                   d2.total_debts,
                   cm.name AS committee_name, 
                   cm.id AS committee_id,
                   cm.type AS committee_type,
                   fd.id AS filed_doc_id,
                   fd.doc_name, 
                   fd.reporting_period_end,
                   fd.reporting_period_begin,
                   fd.received_datetime
                 FROM committees AS cm 
                 LEFT JOIN (
                   SELECT DISTINCT ON (committee_id) 
                     id, 
                     committee_id, 
                     doc_name, 
                     reporting_period_end,
                     reporting_period_begin,
                     received_datetime
                   FROM filed_docs 
                   WHERE doc_name NOT IN (
                     'A-1', 
                     'Statement of Organization', 
                     'Letter/Correspondence',
                     'B-1'
                   ) 
                   ORDER BY committee_id, received_datetime DESC
                 ) AS fd 
                   ON fd.committee_id = cm.id 
                 LEFT JOIN d2_reports AS d2 
                   ON fd.id = d2.filed_doc_id 
               )
            '''
            conn.execute(sa.text(create))
            trans.commit()

    def committeeMoney(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW committee_money')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            create = '''
               CREATE MATERIALIZED VIEW committee_money AS (
                 SELECT 
                   MAX(filings.end_funds_available) AS end_funds_available,
                   MAX(filings.committee_name) AS committee_name,
                   MAX(filings.committee_id) AS committee_id,
                   MAX(filings.committee_type) AS committee_type,
                   MAX(filings.doc_name) AS doc_name,
                   MAX(filings.reporting_period_end) AS reporting_period_end,
                   MAX(filings.reporting_period_begin) AS reporting_period_begin,
                   (SUM(COALESCE(receipts.amount, 0)) + 
                    MAX(COALESCE(filings.end_funds_available, 0)) + 
                    MAX(COALESCE(filings.total_investments, 0)) - 
                    MAX(COALESCE(filings.total_debts, 0))) AS total,
                   MAX(receipts.received_date) AS last_receipt_date
                 FROM most_recent_filings AS filings
                 LEFT JOIN receipts
                   ON receipts.committee_id = filings.committee_id
                   AND receipts.received_date > filings.reporting_period_end
                 GROUP BY filings.committee_id
                 ORDER BY total DESC NULLS LAST
               )
            '''
            conn.execute(sa.text(create))
            trans.commit()
    
    def candidateMoney(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW candidate_money')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            create = '''
                CREATE MATERIALIZED VIEW candidate_money AS (
                  SELECT
                    cd.id AS candidate_id,
                    cd.first_name AS candidate_first_name,
                    cd.last_name AS candidate_last_name,
                    cd.office AS candidate_office,
                    cm.id AS committee_id,
                    cm.name AS committee_name,
                    cm.type AS committee_type,
                    m.total,
                    m.last_receipt_date
                  FROM candidates AS cd
                  JOIN candidate_committees AS cc
                    ON cd.id = cc.candidate_id
                  JOIN committees AS cm
                    ON cc.committee_id = cm.id
                  JOIN committee_money AS m
                    ON cm.id = m.committee_id
                  ORDER BY m.total DESC NULLS LAST
                )
            '''
            conn.execute(sa.text(create))
            trans.commit()
    
    def fullSearchView(self):
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute('REFRESH MATERIALIZED VIEW full_search')
            trans.commit()
        except sa.exc.ProgrammingError:
            trans.rollback()
            conn = self.engine.connect()
            trans = conn.begin()
            
            create = ''' 
                CREATE MATERIALIZED VIEW full_search AS (
                  SELECT 
                    name,
                    table_name,
                    json_agg(record_json) AS records
                  FROM (
                    SELECT 
                      COALESCE(TRIM(TRANSLATE(first_name, '.,-/', '')), '') || ' ' ||
                      COALESCE(TRIM(TRANSLATE(last_name, '.,-/', '')), '') AS name,
                      'candidates' AS table_name,
                      row_to_json(cand) AS record_json
                    FROM candidates AS cand
                    UNION ALL
                    SELECT
                      name,
                      'committees' AS table_name,
                      row_to_json(comm) AS record_json
                    FROM committees AS comm
                    UNION ALL
                    SELECT
                      COALESCE(TRIM(TRANSLATE(first_name, '.,-/', '')), '') || ' ' ||
                      COALESCE(TRIM(TRANSLATE(last_name, '.,-/', '')), '') AS name,
                      'receipts' AS table_name,
                      row_to_json(rec) AS record_json
                    FROM (
                      SELECT
                        r.*,
                        c.name AS committee_name,
                        c.type AS committee_type
                      FROM receipts AS r
                      JOIN committees AS c
                        ON r.committee_id = c.id
                    ) AS rec
                    UNION ALL
                    SELECT
                      COALESCE(TRIM(TRANSLATE(first_name, '.,-/', '')), '') || ' ' ||
                      COALESCE(TRIM(TRANSLATE(last_name, '.,-/', '')), '') AS name,
                      'expenditures' AS table_name,
                      row_to_json(exp) AS record_json
                    FROM (
                      SELECT
                        e.*,
                        c.name AS committee_name,
                        c.type AS committee_type
                      FROM expenditures AS e
                      JOIN committees AS c
                        ON e.committee_id = c.id
                    ) AS exp
                    UNION ALL
                    SELECT
                      COALESCE(TRIM(TRANSLATE(first_name, '.,-/', '')), '') || ' ' ||
                      COALESCE(TRIM(TRANSLATE(last_name, '.,-/', '')), '') AS name,
                      'officers' AS table_name,
                      row_to_json(off) AS record_json
                    FROM (
                      SELECT
                        o.*,
                        c.name AS committee_name,
                        c.type AS committee_type
                      FROM officers AS o
                      JOIN committees AS c
                        ON o.committee_id = c.id
                    ) AS off
                    UNION ALL
                    SELECT
                      COALESCE(TRIM(TRANSLATE(first_name, '.,-/', '')), '') || ' ' ||
                      COALESCE(TRIM(TRANSLATE(last_name, '.,-/', '')), '') AS name,
                      'investments' AS table_name,
                      row_to_json(inv) AS record_json
                    FROM (
                      SELECT
                        i.*,
                        c.name AS committee_name,
                        c.type AS committee_type
                      FROM investments AS i
                      JOIN committees AS c
                        ON i.committee_id = c.id
                    ) AS inv
                  ) AS s
                  GROUP BY table_name, name
                )
            '''
            conn.execute(sa.text(create))
            trans.commit()


class SunshineIndexes(object):
    def __init__(self, engine):
        self.engine = engine

    def makeAllIndexes(self):
        self.fullSearchIndex()
        self.receiptsDate()
        self.receiptsCommittee()

    def fullSearchIndex(self):
        ''' 
        Search names across all tables
        '''
        index = ''' 
            CREATE INDEX name_index ON full_search
            USING gin(to_tsvector('english', name))
        '''
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute(index)
            trans.commit()
        except sa.exc.ProgrammingError as e:
            trans.rollback()
            return

    def receiptsDate(self):
        ''' 
        Make index on received_date for receipts
        '''
        index = ''' 
            CREATE INDEX received_date_idx ON receipts (received_date)
        '''
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute(index)
            trans.commit()
        except sa.exc.ProgrammingError as e:
            trans.rollback()
            return
    
    def receiptsCommittee(self):
        ''' 
        Make index on committee_id for receipts
        '''
        index = ''' 
            CREATE INDEX receipts_committee_idx ON receipts (committee_id)
        '''
        conn = self.engine.connect()
        trans = conn.begin()
        try:
            conn.execute(index)
            trans.commit()
        except sa.exc.ProgrammingError as e:
            trans.rollback()
            return

if __name__ == "__main__":
    import sys
    import argparse
    from sunshine import app_config 
    from sunshine.database import engine, Base

    parser = argparse.ArgumentParser(description='Download and import campaign disclosure data from the IL State Board of Elections.')
    parser.add_argument('--download', action='store_true',
                   help='Downloading fresh data')

    parser.add_argument('--cache', action='store_true',
                   help='Cache downloaded files to S3')

    parser.add_argument('--load_data', action='store_true',
                   help='Load data into database')

    parser.add_argument('--recreate_views', action='store_true',
                   help='Recreate database views')
    
    parser.add_argument('--chunk_size', help='Adjust the size of each insert when loading data',
                   type=int)

    args = parser.parse_args()

    extract = SunshineExtract(ftp_host=app_config.FTP_HOST,
                              ftp_path=app_config.FTP_PATH,
                              ftp_user=app_config.FTP_USER,
                              ftp_pw=app_config.FTP_PW,
                              aws_key=app_config.AWS_KEY,
                              aws_secret=app_config.AWS_SECRET)
    
    if args.download:
        print("downloading ...")
        extract.download(cache=args.cache)
    else:
        print("skipping download")
    
    del extract

    if args.load_data:
        print("loading data ...")
        
        chunk_size = 50000

        if args.chunk_size:
            chunk_size = args.chunk_size

        committees = SunshineCommittees(engine, 
                                        Base.metadata, 
                                        chunk_size=chunk_size)
        committees.load()
        committees.update()
        
        del committees
        del Base.metadata

        candidates = SunshineCandidates(engine, chunk_size=chunk_size)
        candidates.load()
        candidates.update()
        
        del candidates

        officers = SunshineOfficers(engine, chunk_size=chunk_size)
        officers.load()
        officers.update()
        
        del officers

        prev_off = SunshinePrevOfficers(engine, chunk_size=chunk_size)
        prev_off.load()
        prev_off.update()
        
        del prev_off

        candidacy = SunshineCandidacy(engine, chunk_size=chunk_size)
        candidacy.load()
        candidacy.update()
        
        del candidacy

        can_cmte_xwalk = SunshineCandidateCommittees(engine, chunk_size=chunk_size)
        can_cmte_xwalk.load()
        can_cmte_xwalk.update()
        
        del can_cmte_xwalk

        off_cmte_xwalk = SunshineOfficerCommittees(engine, chunk_size=chunk_size)
        off_cmte_xwalk.load()
        off_cmte_xwalk.update()
        
        del off_cmte_xwalk

        filed_docs = SunshineFiledDocs(engine, chunk_size=chunk_size)
        filed_docs.load()
        filed_docs.update()
        
        del filed_docs

        d2_reports = SunshineD2Reports(engine, chunk_size=chunk_size)
        d2_reports.load()
        d2_reports.update()
        
        del d2_reports

        receipts = SunshineReceipts(engine, chunk_size=chunk_size)
        receipts.load()
        receipts.update()
        
        del receipts

        expenditures = SunshineExpenditures(engine, chunk_size=chunk_size)
        expenditures.load()
        expenditures.update()
        
        del expenditures

        investments = SunshineInvestments(engine, chunk_size=chunk_size)
        investments.load()
        investments.update()
        
        del investments

    else:
        print("skipping load")

    views = SunshineViews(engine)

    if args.recreate_views:
        print("dropping views")
        views.dropViews()

    print("creating views ...")
    views.makeAllViews()
    
    print("creating indexes ...")
    indexes = SunshineIndexes(engine)
    indexes.makeAllIndexes()
