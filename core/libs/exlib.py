from core.common.models import JediDatasets, JediDatasetContents, Filestable4, FilestableArch
import math, random, datetime
from django.db import connection
from core.settings.local import dbaccess


def fileList(jobs):
    newjobs = []
    if (len(jobs)>0):
        pandaIDQ = []
        query = {}
        datasetsFromFileTable = []
        JediDatasetContentsTable=[]
        JediDatasetsTable=[]
        datasetIDQ =set()
        for job in jobs:
            pandaIDQ.append(job['pandaid'])
        query['pandaid__in'] = pandaIDQ
        #query['type'] = 'INPUT'
        pandaLFN = {}
        datasetsFromFileTable.extend(
            Filestable4.objects.filter(**query).extra(where=["TYPE like %s"], params=["input"]).values())
        if len(datasetsFromFileTable) == 0:
            datasetsFromFileTable.extend(
                FilestableArch.objects.filter(**query).extra(where=["TYPE like %s"], params=["input"]).values())
        for dataset in datasetsFromFileTable:
            datasetIDQ.add(dataset['datasetid'])
            pandaLFN.setdefault(dataset['pandaid'],[]).append(dataset['lfn'])

        query['datasetid__in'] = list(datasetIDQ)
        JediDatasetContentsTable.extend(JediDatasetContents.objects.filter(**query).extra(where=["datasetid in (SELECT datasetid from ATLAS_PANDA.JEDI_DATASETS where masterid is NULL)"]).values())
        njob = {}

        for jds in JediDatasetContentsTable:
            if jds['pandaid'] not in njob:
                njob[jds['pandaid']] = {}
                if jds['nevents'] == None:
                    njob[jds['pandaid']]['nevents'] = 0
                else:
                    if jds['endevent']!=None and jds['startevent']!=None:
                        njob[jds['pandaid']]['nevents'] = int(jds['endevent'])+1-int(jds['startevent'])
                    else: njob[jds['pandaid']]['nevents'] = int(jds['nevents'])
                if jds['type']=='input':
                    njob[jds['pandaid']]['ninputs'] = []
                    njob[jds['pandaid']]['ninputs'].append(jds['lfn'])
            else:
                if jds['endevent'] != None and jds['startevent'] != None:
                    njob[jds['pandaid']]['nevents'] += int(jds['endevent']) + 1 - int(jds['startevent'])
                else:
                    if jds['nevents'] != None:
                        njob[jds['pandaid']]['nevents'] += int(jds['nevents'])
                    else: njob[jds['pandaid']]['nevents'] += 0
                if jds['type']=='input':
                    njob[jds['pandaid']]['ninputs'].append(jds['lfn'])
        listpandaidsDS = njob.keys()
        listpandaidsF4 = pandaLFN.keys()
        for job in jobs:
            if job['pandaid'] in listpandaidsDS:
                # job['nevents'] = int(math.fabs(job['nevents']-njob[job['pandaid']]['nevents']))
                job['nevents'] = njob[job['pandaid']]['nevents']
                if 'ninputs' in njob[job['pandaid']]:
                    job['ninputs'] = len(njob[job['pandaid']]['ninputs'])
                else:
                    job['ninputs'] = 0 #0
                    if job['processingtype'] == 'pmerge':
                        if len(job['jobinfo']) == 0:
                            job['jobinfo'] = 'Pmerge job'
                        else:
                            job['jobinfo'] += 'Pmerge job'
            else:
                #if isEventService(job):
                if job['pandaid'] in listpandaidsF4:
                    job['ninputs'] = len(pandaLFN[job['pandaid']])
                else: job['ninputs'] = 0
            if job['pandaid'] in listpandaidsF4 and 'ninputs' not in job:
                 job['ninputs'] = len(pandaLFN[job['pandaid']])
            #print str(job['pandaid'])+' '+str(job['ninputs'])
            #if job['ninputs']==0:
            #    job['ninputs'] = len(pandaLFN[job['pandaid']])
                #else: job['ninputs'] = len(pandaLFN[job['pandaid']])
            # if (job['jobstatus'] == 'finished' and job['nevents'] == 0) or (job['jobstatus'] == 'cancelled' and job['nevents'] == 0):
            #     job['ninputs'] = 0
            # if job['jobstatus'] == 'finished' and job['pandaid'] not in listpandaidsDS:
            #     job['ninputs'] = 0
            #     job['nevents'] = 0
            if job['nevents']!= 0 and 'ninputs' not in job and job['pandaid'] in listpandaidsF4:
                job['ninputs'] = len(pandaLFN[job['pandaid']])

    newjobs = jobs
    return newjobs


def insert_to_temp_table(list_of_items, transactionKey = -1):
    """Inserting to temp table
    :param list_of_items
    :return transactionKey and timestamp of instering
    """

    # if dbaccess['default']['ENGINE'].find('oracle') >= 0:
    #     tmpTableName = "ATLAS_PANDABIGMON.TMP_IDS1"
    # else:
    #     tmpTableName = "TMP_IDS1"

    if transactionKey == -1:
        random.seed()
        transactionKey = random.randrange(1000000)

    new_cur = connection.cursor()
    executionData = []
    for item in list_of_items:
        executionData.append((item, transactionKey))
    query = """INSERT INTO ATLAS_PANDABIGMON.TMP_IDS1Debug(ID,TRANSACTIONKEY) VALUES (%s,%s)"""
    new_cur.executemany(query, executionData)

    return transactionKey


def dictfetchall(cursor):
    "Returns all rows from a cursor as a dict"
    desc = cursor.description
    return [
        dict(zip([col[0] for col in desc], row))
        for row in cursor.fetchall()
        ]