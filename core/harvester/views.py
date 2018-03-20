from collections import OrderedDict

from datetime import datetime

from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import render_to_response
from pandas import json
from django.db import connection
import json
from core.libs.cache import setCacheEntry, getCacheEntry
from core.views import login_customrequired, initRequest, setupView, endSelfMonitor, escapeInput, DateEncoder
from core.harvester.models import HarvesterWorkers,HarvesterRelJobsWorkers

harvWorkStatuses = [
    'missed', 'submitted', 'ready', 'running', 'idle', 'finished', 'failed', 'cancelled'
]


@login_customrequired
def harvesterWorkersDash(request):
    valid, response = initRequest(request)

    hours = 24 * 3
    if 'days' in request.session['requestParams']:
        days = int(request.session['requestParams']['days'])
        hours = days*24
    query= setupView(request, hours=hours, wildCardExt=False)


    tquery = {}
    tquery['status__in'] = ['missed', 'submitted', 'idle', 'finished', 'failed', 'cancelled']
    tquery['lastupdate__range'] = query['modificationtime__range']
    if 'harvesterid__in' in query:
        tquery['harvesterid__in'] = query['harvesterid__in']

    harvesterWorkers = []
    harvesterWorkers.extend(HarvesterWorkers.objects.values('computingsite','status').filter(**tquery).annotate(Count('status')).order_by('computingsite'))

    # This is for exclusion of intermediate states from time window
    tquery['status__in'] = ['ready', 'running']
    del tquery['lastupdate__range']
    harvesterWorkers.extend(HarvesterWorkers.objects.values('computingsite','status').filter(**tquery).annotate(Count('status')).order_by('computingsite'))

    statusesSummary = OrderedDict()
    for harvesterWorker in harvesterWorkers:
        if not harvesterWorker['computingsite'] in statusesSummary:
            statusesSummary[harvesterWorker['computingsite']] = OrderedDict()
            for harwWorkStatus in harvWorkStatuses:
                statusesSummary[harvesterWorker['computingsite']][harwWorkStatus] = 0
        statusesSummary[harvesterWorker['computingsite']][harvesterWorker['status']] = harvesterWorker['status__count']

    # SELECT computingsite,status, workerid, LASTUPDATE, row_number() over (partition by workerid, computingsite ORDER BY LASTUPDATE ASC) partid FROM ATLAS_PANDA.HARVESTER_WORKERS /*GROUP BY WORKERID ORDER BY COUNT(WORKERID) DESC*/

    data = {
        'statusesSummary': statusesSummary,
        'harvWorkStatuses':harvWorkStatuses,
        'request': request,
        'hours':hours,
        'viewParams': request.session['viewParams'],
        'requestParams': request.session['requestParams'],
        'built': datetime.now().strftime("%H:%M:%S"),
    }
    endSelfMonitor(request)
    response = render_to_response('harvworksummarydash.html', data, content_type='text/html')
    return response


# SELECT COMPUTINGSITE,STATUS, count(*) FROM ATLAS_PANDA.HARVESTER_WORKERS WHERE SUBMITTIME > (sysdate - interval '35' day) group by COMPUTINGSITE,STATUS
@login_customrequired
def harvesterWorkList(request):
    valid, response = initRequest(request)
    query,extra, LAST_N_HOURS_MAX = setupView(request, hours=24*3, wildCardExt=True)

    statusDefined = False
    if 'status__in' in query:
        statusDefined = True

    tquery = {}

    if statusDefined:
        tquery['status__in'] = list(set(query['status__in']).intersection(['missed', 'submitted', 'idle', 'finished', 'failed', 'cancelled']))
    else:
        tquery['status__in'] = ['missed', 'submitted', 'idle', 'finished', 'failed', 'cancelled']

    tquery['lastupdate__range'] = query['modificationtime__range']

    workerslist = []
    if len(tquery['status__in']) > 0:
        workerslist.extend(HarvesterWorkers.objects.values('computingsite','status', 'submittime','harvesterid','workerid').filter(**tquery).extra(where=[extra]))

    if statusDefined:
        tquery['status__in'] = list(set(query['status__in']).intersection(['ready', 'running']))

    del tquery['lastupdate__range']
    if len(tquery['status__in']) > 0:
        workerslist.extend(HarvesterWorkers.objects.values('computingsite','status', 'submittime','harvesterid','workerid').filter(**tquery).extra(where=[extra]))

    data = {
        'workerslist':workerslist,
        'request': request,
        'viewParams': request.session['viewParams'],
        'requestParams': request.session['requestParams'],
        'built': datetime.now().strftime("%H:%M:%S"),
    }
    endSelfMonitor(request)
    response = render_to_response('harvworkerslist.html', data, content_type='text/html')
    return response

@login_customrequired
def harvesterWorkerInfo(request):
    valid, response = initRequest(request)
    harvesterid = None
    workerid = None
    workerinfo = {}

    if 'harvesterid' in request.session['requestParams']:
        harvesterid = escapeInput(request.session['requestParams']['harvesterid'])
    if 'workerid' in request.session['requestParams']:
        workerid = int(request.session['requestParams']['workerid'])

    workerslist = []
    error = None
    if harvesterid and workerid:
        tquery = {}
        tquery['harvesterid'] = harvesterid
        tquery['workerid'] = workerid
        workerslist.extend(HarvesterWorkers.objects.filter(**tquery).values('harvesterid','workerid',
                                                                            'lastupdate','status','batchid','nodeid',
                                                                            'queuename', 'computingsite','submittime',
                                                                            'starttime','endtime','ncore','errorcode',
                                                                            'stdout','stderr','batchlog'))

        if len(workerslist) > 0:
            corrJobs = []
            corrJobs.extend(HarvesterRelJobsWorkers.objects.filter(**tquery).values('pandaid'))
            workerinfo = workerslist[0]
            workerinfo['corrJobs'] = []
            for corrJob in corrJobs:
                workerinfo['corrJobs'].append(corrJob['pandaid'])
        else:
            workerinfo = None
    else:
        error = "Harvesterid + Workerid is not specified"

    data = {
        'request': request,
        'error': error,
        'workerinfo': workerinfo,
        'viewParams': request.session['viewParams'],
        'requestParams': request.session['requestParams'],
        'built': datetime.now().strftime("%H:%M:%S"),
    }

    endSelfMonitor(request)
    response = render_to_response('harvworkerinfo.html', data, content_type='text/html')
    return response

def harvesterfm(request):
    valid, response = initRequest(request)
    if 'instance' in request.session['requestParams'] and 'workerid' not in request.session['requestParams']:
        instance = request.session['requestParams']['instance']
        if (('HTTP_ACCEPT' in request.META) and (request.META.get('HTTP_ACCEPT') in ('text/json', 'application/json'))) or ('json' in request.session['requestParams']):
            data = getCacheEntry(request, instance,isData=True)
            import json
            return HttpResponse(data, content_type='text/html')




        sqlquery = """
    select * from (SELECT
    ff.harvester_id,
    ff.description,
    to_char(ff.starttime, 'dd-mm-yyyy hh24:mi:ss') as "insstarttime",
    ff.owner,
    ff.hostname,
    gg.workerid,
    (select max(lastupdate) from atlas_panda.harvester_workers where harvesterid like '%s') as "inslastupdate",
    gg.status,
    gg.batchid,
    gg.nodeid,
    gg.queuename,
    gg.computingsite,
    gg.submittime,
    gg.lastupdate as "wrklastupdate",
    gg.starttime as "wrkstarttime",
    gg.endtime as "wrkendtime",
    gg.ncore,
    gg.errorcode,
    gg.stdout,
    gg.stderr,
    gg.batchlog,
    gg.resourcetype,
    gg.nativeexitcode,
    gg.nativestatus,
    gg.diagmessage
FROM
    atlas_panda.harvester_workers gg,
    atlas_panda.harvester_instances ff
WHERE
    ff.harvester_id = gg.harvesterid) where harvester_id like '%s' 
    order by workerid DESC
        """ % (str(instance), str(instance))
        workersList = []

        cur = connection.cursor()
        cur.execute(sqlquery)
        columns = [str(i[0]).lower() for i in cur.description]
        workers = cur.fetchall()
        for worker in workers:
            object = {}
            for i, col in enumerate(columns):
                object[col] = worker[i]
            workersList.append(object)
        statuses = {}
        computingsites = {}

        generalInstanseInfo = {}

        generalWorkersFields = ['workerid','status','batchid','nodeid','queuename','computingsite','submittime','wrklastupdate','wrkstarttime','wrkendtime','ncore','errorcode','stdout','stderr','batchlog']
        generalWorkersList = []
        # for worker in workersList:
        for i, worker in enumerate(workersList):
            object = {}
            computingsites.setdefault(worker['computingsite'],[]).append(worker['workerid'])
            statuses.setdefault(worker['status'],[]).append(worker['workerid'])
            for field in generalWorkersFields:
                object[field] = worker[field]
            generalWorkersList.append(object)
            if i == len(workersList) - 1:
                for computingsite in computingsites.keys():
                    computingsites[computingsite] = len(computingsites[computingsite])
                for status in statuses.keys():
                    statuses[status] = len(statuses[status])
                generalInstanseInfo = {'HarvesterID':worker['harvester_id'], 'Description':worker['description'], 'Starttime': worker['insstarttime'],
                                      'Owner':worker['owner'], 'Hostname':worker['hostname'],'Lastupdate':worker['inslastupdate'], 'Computingsites':computingsites,'Statuses':statuses
                                      }
                data = {
                    'generalInstanseInfo':generalInstanseInfo,
                    'type':'workers',
                    'instance':instance
                }
                import json
                if 'display_limit_workers' in request.session['requestParams']:
                    display_limit_workers = int(request.session['requestParams']['display_limit_workers'])
                else:
                    display_limit_workers = 30000

                setCacheEntry(request, instance, json.dumps(generalWorkersList[:display_limit_workers], cls=DateEncoder), 60 * 60,isData=True)

        return render_to_response('harvesterfm.html', data, content_type='text/html')

    elif 'instance' in request.session['requestParams'] and 'workerid' in 'instance' in request.session['requestParams']:
        pass
    else:
        sqlquery = """
  select  
    R.harvid,
    count(R.workid) as total,
    
    (select cnt from   (select harvid, count(*) as cnt from (
    SELECT
    a.harvester_id as harvid, 
    b.workerid as workid,
    to_char(b.lastupdate, 'dd-mm-yyyy hh24:mi:ss') as alldate,
    (SELECT
    to_char(max(O.lastupdate), 'dd-mm-yyyy hh24:mi:ss')
    FROM atlas_panda.harvester_rel_jobs_workers O where  O.harvesterid = a.harvester_id   Group by O.harvesterid) as recently, 

    a.DESCRIPTION as description
    FROM
     atlas_panda.harvester_rel_jobs_workers b,
    atlas_panda.harvester_instances a
    WHERE a.harvester_id = b.harvesterid
    ) where alldate = recently Group by harvid) W where W.harvid=R.harvid) as recent,
    R.recently,
    
    R.description
    
    FROM (SELECT
    a.harvester_id as harvid, 
    b.workerid as workid,
    to_char(b.lastupdate, 'dd-mm-yyyy hh24:mi:ss') as alldate,
    (SELECT
    to_char(max(O.lastupdate), 'dd-mm-yyyy hh24:mi:ss')
    FROM atlas_panda.harvester_rel_jobs_workers O where  O.harvesterid = a.harvester_id   Group by O.harvesterid) as recently, 

    a.DESCRIPTION as description
    FROM
    atlas_panda.harvester_workers b,
    atlas_panda.harvester_instances a
    WHERE a.harvester_id = b.harvesterid) R group by harvid, description,recently
        """
        instanceDicList = []
        cur = connection.cursor()
        cur.execute(sqlquery)
        instancesList = cur.fetchall()
        for ins in instancesList:
            instanceDicList.append(
                {'instance': ins[0], 'total': ins[1], 'recently': ins[2], 'when': ins[3], 'descr': ins[4]})
        import json
        data = {
            'instances':instanceDicList,
            'type': 'instances'
        }
        #data =json.dumps(data,cls=DateEncoder)
        response = render_to_response('harvesterfm.html', data, content_type='text/html')
    return response