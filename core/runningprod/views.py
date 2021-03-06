import random
import json
import copy
from collections import OrderedDict

from datetime import datetime, timedelta

from django.utils import timezone
from django.db.models import Count
from django.http import HttpResponse
from django.shortcuts import render_to_response, redirect
from django.db import connection, transaction, DatabaseError

from django.utils.cache import patch_cache_control, patch_response_headers


from core.settings import STATIC_URL, FILTER_UI_ENV, defaultDatetimeFormat
from core.libs.cache import deleteCacheTestData, getCacheEntry, setCacheEntry, preparePlotData
from core.views import login_customrequired, initRequest, setupView, endSelfMonitor, escapeInput, DateEncoder, \
    extensibleURL, DateTimeEncoder, removeParam, taskSummaryDict, preprocessWildCardString

from core.runningprod.models import RunningDPDProductionTasks, RunningProdTasksModel , RunningMCProductionTasks,  RunningProdRequestsModel, FrozenProdTasksModel, ProdNeventsHistory

def prepareNeventsByProcessingType(task_list):
    """
    Prepare data to save
    :param task_list:
    :return:
    """

    event_states = ['total', 'used', 'running', 'waiting']
    neventsByProcessingType = {}
    for task in task_list:
        if task['processingtype'] not in neventsByProcessingType:
            neventsByProcessingType[task['processingtype']] = {}
            for ev in event_states:
                neventsByProcessingType[task['processingtype']][ev] = 0
        neventsByProcessingType[task['processingtype']]['total'] += task['nevents'] if 'nevents' in task and task['nevents'] is not None else 0
        neventsByProcessingType[task['processingtype']]['used'] += task['neventsused'] if 'neventsused' in task and task['neventsused'] is not None else 0
        neventsByProcessingType[task['processingtype']]['running'] += task['neventsrunning'] if 'neventsrunning' in task and task['neventsrunning'] is not None else 0
        neventsByProcessingType[task['processingtype']]['waiting'] += (task['neventstobeused'] - task['neventsrunning']) if 'neventstobeused' in task and 'neventsrunning' in task and task['neventstobeused'] is not None and task['neventsrunning'] is not None else 0

    return neventsByProcessingType

def saveNeventsByProcessingType(neventsByProcessingType, qtime):
    """
    Save a snapshot of production state expressed in nevents in different states for various processingtype
    :param neventsByProcessingType:
    :param qtime:
    :return: True in case of successful save but False in case of an exception error
    """

    try:
        with transaction.atomic():
            for pt, data in neventsByProcessingType.iteritems():
                row = ProdNeventsHistory(processingtype=pt,
                                         neventstotal=data['total'],
                                         neventsused=data['used'],
                                         neventswaiting=data['waiting'],
                                         neventsrunning=data['running'],
                                         timestamp=qtime)
                row.save()
    except DatabaseError as e:
        print e.message
        return False

    return True

@login_customrequired
def runningMCProdTasks(request):
    # redirect to united runningProdTasks page
    return redirect('/runningprodtasks/?preset=MC')
    valid, response = initRequest(request)

    # Here we try to get cached data
    data = getCacheEntry(request, "runningMCProdTasks")
    if data is not None:
        data = json.loads(data)
        data['request'] = request
        response = render_to_response('runningMCProdTasks.html', data, content_type='text/html')
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        endSelfMonitor(request)
        return response


    # xurl = extensibleURL(request)
    xurl = request.get_full_path()
    if xurl.find('?') > 0:
        xurl += '&'
    else:
        xurl += '?'
    nosorturl = removeParam(xurl, 'sortby', mode='extensible')
    tquery, wildCardExtension, LAST_N_HOURS_MAX = setupView(request, hours=0, limit=9999999, querytype='task',
                                                           wildCardExt=True)

    tasks = RunningMCProductionTasks.objects.filter(**tquery).extra(where=[wildCardExtension]).values()
    ntasks = len(tasks)
    slots = 0
    ages = []
    neventsAFIItasksSum = {'evgen': 0, 'pile': 0, 'simul': 0, 'recon': 0}
    neventsFStasksSum = {'evgen': 0, 'pile': 0, 'simul': 0, 'recon': 0}

    neventsTotSum = 0
    neventsUsedTotSum = 0
    rjobs1coreTot = 0
    rjobs8coreTot = 0
    for task in tasks:
        if task['rjobs'] is None:
            task['rjobs'] = 0
        task['neventsused'] = task['totev'] - task['totevrem'] if task['totev'] is not None else 0
        task['percentage'] = round(100. * task['neventsused'] / task['totev'], 1) if task['totev'] > 0 else 0.
        neventsTotSum += task['totev'] if task['totev'] is not None else 0
        neventsUsedTotSum += task['neventsused']
        slots += task['rjobs'] * task['corecount']
        if task['corecount'] == 1:
            rjobs1coreTot += task['rjobs']
        if task['corecount'] == 8:
            rjobs8coreTot += task['rjobs']
        task['age'] = (datetime.now() - task['creationdate']).days
        ages.append(task['age'])
        if len(task['campaign'].split(':')) > 1:
            task['cutcampaign'] = task['campaign'].split(':')[1]
        else:
            task['cutcampaign'] = task['campaign'].split(':')[0]
        task['datasetname'] = task['taskname'].split('.')[1]
        ltag = len(task['taskname'].split("_"))
        rtag = task['taskname'].split("_")[ltag - 1]
        if "." in rtag:
            rtag = rtag.split(".")[len(rtag.split(".")) - 1]
        if 'a' in rtag:
            task['simtype'] = 'AFII'
            neventsAFIItasksSum[task['processingtype']] += task['totev'] if task['totev'] is not None else 0
        else:
            task['simtype'] = 'FS'
            neventsFStasksSum[task['processingtype']] += task['totev'] if task['totev'] is not None else 0
    plotageshistogram = 1
    if sum(ages) == 0: plotageshistogram = 0
    sumd = taskSummaryDict(request, tasks, ['status', 'processingtype', 'simtype'])

    if 'sortby' in request.session['requestParams']:
        sortby = request.session['requestParams']['sortby']
        if sortby == 'campaign-asc':
            tasks = sorted(tasks, key=lambda x: x['campaign'])
        elif sortby == 'campaign-desc':
            tasks = sorted(tasks, key=lambda x: x['campaign'], reverse=True)
        elif sortby == 'reqid-asc':
            tasks = sorted(tasks, key=lambda x: x['reqid'])
        elif sortby == 'reqid-desc':
            tasks = sorted(tasks, key=lambda x: x['reqid'], reverse=True)
        elif sortby == 'jeditaskid-asc':
            tasks = sorted(tasks, key=lambda x: x['jeditaskid'])
        elif sortby == 'jeditaskid-desc':
            tasks = sorted(tasks, key=lambda x: x['jeditaskid'], reverse=True)
        elif sortby == 'rjobs-asc':
            tasks = sorted(tasks, key=lambda x: x['rjobs'])
        elif sortby == 'rjobs-desc':
            tasks = sorted(tasks, key=lambda x: x['rjobs'], reverse=True)
        elif sortby == 'status-asc':
            tasks = sorted(tasks, key=lambda x: x['status'])
        elif sortby == 'status-desc':
            tasks = sorted(tasks, key=lambda x: x['status'], reverse=True)
        elif sortby == 'processingtype-asc':
            tasks = sorted(tasks, key=lambda x: x['processingtype'])
        elif sortby == 'processingtype-desc':
            tasks = sorted(tasks, key=lambda x: x['processingtype'], reverse=True)
        elif sortby == 'nevents-asc':
            tasks = sorted(tasks, key=lambda x: x['totev'])
        elif sortby == 'nevents-desc':
            tasks = sorted(tasks, key=lambda x: x['totev'], reverse=True)
        elif sortby == 'neventsused-asc':
            tasks = sorted(tasks, key=lambda x: x['neventsused'])
        elif sortby == 'neventsused-desc':
            tasks = sorted(tasks, key=lambda x: x['neventsused'], reverse=True)
        elif sortby == 'neventstobeused-asc':
            tasks = sorted(tasks, key=lambda x: x['totevrem'])
        elif sortby == 'neventstobeused-desc':
            tasks = sorted(tasks, key=lambda x: x['totevrem'], reverse=True)
        elif sortby == 'percentage-asc':
            tasks = sorted(tasks, key=lambda x: x['percentage'])
        elif sortby == 'percentage-desc':
            tasks = sorted(tasks, key=lambda x: x['percentage'], reverse=True)
        elif sortby == 'nfilesfailed-asc':
            tasks = sorted(tasks, key=lambda x: x['nfilesfailed'])
        elif sortby == 'nfilesfailed-desc':
            tasks = sorted(tasks, key=lambda x: x['nfilesfailed'], reverse=True)
        elif sortby == 'priority-asc':
            tasks = sorted(tasks, key=lambda x: x['currentpriority'])
        elif sortby == 'priority-desc':
            tasks = sorted(tasks, key=lambda x: x['currentpriority'], reverse=True)
        elif sortby == 'simtype-asc':
            tasks = sorted(tasks, key=lambda x: x['simtype'])
        elif sortby == 'simtype-desc':
            tasks = sorted(tasks, key=lambda x: x['simtype'], reverse=True)
        elif sortby == 'age-asc':
            tasks = sorted(tasks, key=lambda x: x['age'])
        elif sortby == 'age-desc':
            tasks = sorted(tasks, key=lambda x: x['age'], reverse=True)
        elif sortby == 'corecount-asc':
            tasks = sorted(tasks, key=lambda x: x['corecount'])
        elif sortby == 'corecount-desc':
            tasks = sorted(tasks, key=lambda x: x['corecount'], reverse=True)
        elif sortby == 'username-asc':
            tasks = sorted(tasks, key=lambda x: x['username'])
        elif sortby == 'username-desc':
            tasks = sorted(tasks, key=lambda x: x['username'], reverse=True)
        elif sortby == 'datasetname-asc':
            tasks = sorted(tasks, key=lambda x: x['datasetname'])
        elif sortby == 'datasetname-desc':
            tasks = sorted(tasks, key=lambda x: x['datasetname'], reverse=True)
    else:
        sortby = 'age-asc'
        tasks = sorted(tasks, key=lambda x: x['age'])

    if (('HTTP_ACCEPT' in request.META) and (request.META.get('HTTP_ACCEPT') in ('text/json', 'application/json'))) or (
        'json' in request.session['requestParams']):

        dump = json.dumps(tasks, cls=DateEncoder)
        ##self monitor
        endSelfMonitor(request)
        return HttpResponse(dump, content_type='text/html')
    else:
        data = {
            'request': request,
            'viewParams': request.session['viewParams'],
            'requestParams': request.session['requestParams'],
            'xurl': xurl,
            'nosorturl': nosorturl,
            'tasks': tasks,
            'ntasks': ntasks,
            'sortby': sortby,
            'ages': ages,
            'slots': slots,
            'sumd': sumd,
            'neventsUsedTotSum': round(neventsUsedTotSum / 1000000., 1),
            'neventsTotSum': round(neventsTotSum / 1000000., 1),
            'rjobs1coreTot': rjobs1coreTot,
            'rjobs8coreTot': rjobs8coreTot,
            'neventsAFIItasksSum': neventsAFIItasksSum,
            'neventsFStasksSum': neventsFStasksSum,
            'plotageshistogram': plotageshistogram,
            'built': datetime.now().strftime("%H:%M:%S"),
        }
        ##self monitor
        endSelfMonitor(request)
        setCacheEntry(request, "runningMCProdTasks", json.dumps(data, cls=DateEncoder), 60 * 20)
        response = render_to_response('runningMCProdTasks.html', data, content_type='text/html')
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        return response


@login_customrequired
def runningProdTasks(request):
    valid, response = initRequest(request)

    if ('dt' in request.session['requestParams'] and 'tk' in request.session['requestParams']):
        tk = request.session['requestParams']['tk']
        data = getCacheEntry(request, tk, isData=True)
        return HttpResponse(data, content_type='text/html')
    # Here we try to get cached data
    data = getCacheEntry(request, "runningProdTasks")
    # data = None
    if data is not None:
        data = json.loads(data)
        data['request'] = request
        if 'ages' in data:
            data['ages'] = preparePlotData(data['ages'])
        if 'neventsFStasksSum' in data:
            data['neventsFStasksSum'] = preparePlotData(data['neventsFStasksSum'])
        if 'neventsAFIItasksSum' in data:
            data['neventsAFIItasksSum'] = preparePlotData(data['neventsAFIItasksSum'])
        if 'neventsByProcessingType' in data:
            data['neventsByProcessingType'] = preparePlotData(data['neventsByProcessingType'])
        if 'aslotsByType' in data:
            data['aslotsByType'] = preparePlotData(data['aslotsByType'])
        if 'neventsByTaskStatus' in data:
            data['neventsByTaskStatus'] = preparePlotData(data['neventsByTaskStatus'])
        if 'neventsByTaskPriority' in data:
            data['neventsByTaskPriority'] = preparePlotData(data['neventsByTaskPriority'])
        if 'neventsByStatus' in data:
            data['neventsByStatus'] = preparePlotData(data['neventsByStatus'])
        response = render_to_response('runningProdTasks.html', data, content_type='text/html')
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        endSelfMonitor(request)
        return response


    # xurl = extensibleURL(request)
    xurl = request.get_full_path()
    if xurl.find('?') > 0:
        xurl += '&'
    else:
        xurl += '?'
    nosorturl = removeParam(xurl, 'sortby', mode='extensible')
    nohashtagurl = removeParam(xurl, 'hashtags', mode='extensible')
    exquery = {}

    productiontype = ''
    if 'preset' in request.session['requestParams']:
        if request.session['requestParams']['preset'] and request.session['requestParams']['preset'].upper() == 'MC':
            productiontype = 'MC'
            if 'workinggroup' not in request.session['requestParams']:
                request.session['requestParams']['workinggroup'] = '!AP_REPR,!AP_VALI,!GP_PHYS,!GP_THLT'
            if 'processingtype' not in request.session['requestParams']:
                request.session['requestParams']['processingtype'] = 'evgen|pile|simul|recon'
            if 'campaign' not in request.session['requestParams']:
                request.session['requestParams']['campaign'] = 'mc*'
        if request.session['requestParams']['preset'] and request.session['requestParams']['preset'].upper() == 'DPD':
            productiontype = 'DPD'
            if 'workinggroup' not in request.session['requestParams']:
                request.session['requestParams']['workinggroup'] = 'GP_*'
            if 'processingtype' not in request.session['requestParams']:
                request.session['requestParams']['processingtype'] = 'merge|deriv'
        if request.session['requestParams']['preset'] and request.session['requestParams']['preset'].upper() == 'DATA':
            productiontype = 'DATA'
            if 'workinggroup' not in request.session['requestParams']:
                request.session['requestParams']['workinggroup'] = 'AP_REPR'
            if 'processingtype' not in request.session['requestParams']:
                request.session['requestParams']['processingtype'] = 'reprocessing'

    tquery, wildCardExtension, LAST_N_HOURS_MAX = setupView(request, hours=0, limit=9999999, querytype='task',
                                                            wildCardExt=True)

    if 'workinggroup' in tquery and 'preset' in request.session['requestParams'] and request.session['requestParams']['preset'] == 'MC' and ',' in tquery['workinggroup']:
        #     excludeWGList = list(str(wg[1:]) for wg in request.session['requestParams']['workinggroup'].split(','))
        #     exquery['workinggroup__in'] = excludeWGList
        try:
            del tquery['workinggroup']
        except:
            pass
    if 'status' in request.session['requestParams'] and request.session['requestParams']['status'] == '':
        try:
            del tquery['status']
        except:
            pass
    if 'site' in request.session['requestParams'] and request.session['requestParams']['site'] == 'hpc':
        try:
            del tquery['site']
        except:
            pass
        exquery['site__isnull'] = True
    if 'simtype' in request.session['requestParams'] and request.session['requestParams']['simtype']:
            tquery['simtype'] = request.session['requestParams']['simtype']
    if 'runnumber' in request.session['requestParams'] and request.session['requestParams']['runnumber']:
            tquery['runnumber'] = request.session['requestParams']['runnumber']
    if 'ptag' in request.session['requestParams'] and request.session['requestParams']['ptag']:
            tquery['ptag'] = request.session['requestParams']['ptag']
    if 'hashtags' in request.session['requestParams']:
        wildCardExtension += ' AND ('
        wildCards = request.session['requestParams']['hashtags'].split(',')
        currentCardCount = 1
        countCards = len(wildCards)
        for card in wildCards:
            if '*' not in card:
                card = '*' + card + '*'
            elif card.startswith('*'):
                card = card + '*'
            elif card.endswith('*'):
                card = '*' + card
            wildCardExtension += preprocessWildCardString(card, 'hashtags')
            if (currentCardCount < countCards): wildCardExtension += ' AND '
            currentCardCount += 1
        wildCardExtension += ')'
    if 'jumbo' in request.session['requestParams'] and request.session['requestParams']['jumbo']:
        tquery['jumbo'] = request.session['requestParams']['jumbo']
    if 'sortby' in request.session['requestParams'] and '-' in request.session['requestParams']['sortby'] :
        sortby = request.session['requestParams']['sortby']
    else:
        sortby = 'creationdate-desc'
    oquery = '-' + sortby.split('-')[0] if sortby.split('-')[1].startswith('d') else sortby.split('-')[0]

    tasks = []
#    if "((UPPER(status)  LIKE UPPER('all')))" in wildCardExtension and tquery['eventservice'] == 1:
    if 'eventservice' in tquery and tquery['eventservice'] == 1 and 'days' in request.session['requestParams']:

        setupView(request)
        if 'status__in' in tquery:
            del tquery['status__in']
        excludedTimeQuery = copy.deepcopy(tquery)

        if ('days' in request.GET) and (request.GET['days']):
            days = int(request.GET['days'])
            hours = 24 * days
            startdate = timezone.now() - timedelta(hours=hours)
            startdate = startdate.strftime(defaultDatetimeFormat)
            enddate = timezone.now().strftime(defaultDatetimeFormat)
            tquery['modificationtime__castdate__range'] = [startdate, enddate]

        if "((UPPER(status)  LIKE UPPER('all')))" in wildCardExtension:
            wildCardExtension = wildCardExtension.replace("((UPPER(status)  LIKE UPPER('all')))", "(1=1)")
        tasks.extend(RunningProdTasksModel.objects.filter(**excludedTimeQuery).extra(where=[wildCardExtension]).exclude(
            **exquery).values().annotate(nonetoend=Count(sortby.split('-')[0])).order_by('-nonetoend', oquery)[:])
        tasks.extend(FrozenProdTasksModel.objects.filter(**tquery).extra(where=[wildCardExtension]).exclude(
            **exquery).values().annotate(nonetoend=Count(sortby.split('-')[0])).order_by('-nonetoend', oquery)[:])
    else:
        tasks.extend(RunningProdTasksModel.objects.filter(**tquery).extra(where=[wildCardExtension]).exclude(**exquery).values().annotate(nonetoend=Count(sortby.split('-')[0])).order_by('-nonetoend', oquery))

    qtime = datetime.now()
    task_list = [t for t in tasks]
    ntasks = len(tasks)
    slots = 0
    aslots = 0
    ages = []
    neventsAFIItasksSum = {}
    neventsFStasksSum = {}
    neventsByProcessingType = {}
    neventsByTaskStatus = {}
    neventsByTaskPriority = {}
    aslotsByType = {}
    neventsTotSum = 0
    neventsUsedTotSum = 0
    neventsToBeUsedTotSum = 0
    neventsRunningTotSum = 0
    rjobs1coreTot = 0
    rjobs8coreTot = 0
    for task in task_list:
        task['rjobs'] = 0 if task['rjobs'] is None else task['rjobs']
        task['percentage'] = 0 if task['percentage'] is None else round(100 * task['percentage'],1)
        neventsTotSum += task['nevents'] if task['nevents'] is not None else 0
        neventsUsedTotSum += task['neventsused'] if 'neventsused' in task and task['neventsused'] is not None else 0
        neventsToBeUsedTotSum += task['neventstobeused'] if 'neventstobeused' in task and task['neventstobeused'] is not None else 0
        neventsRunningTotSum += task['neventsrunning'] if 'neventsrunning' in task and task['neventsrunning'] is not None else 0
        slots += task['slots'] if task['slots'] else 0
        aslots += task['aslots'] if task['aslots'] else 0
        if not task['processingtype'] in aslotsByType.keys():
            aslotsByType[str(task['processingtype'])] = 0
        aslotsByType[str(task['processingtype'])] += task['aslots'] if task['aslots'] else 0

        if not task['status'] in neventsByTaskStatus.keys():
            neventsByTaskStatus[str(task['status'])] = 0
        neventsByTaskStatus[str(task['status'])] += task['nevents'] if task['nevents'] is not None else 0

        if not task['priority'] in neventsByTaskPriority.keys():
            neventsByTaskPriority[task['priority']] = 0
        neventsByTaskPriority[task['priority']] += task['nevents'] if task['nevents'] is not None else 0

        if task['corecount'] == 1:
            rjobs1coreTot += task['rjobs']
        if task['corecount'] == 8:
            rjobs8coreTot += task['rjobs']
        task['age'] = round(
            (datetime.now() - task['creationdate']).days + (datetime.now() - task['creationdate']).seconds / 3600. / 24,
            1)
        ages.append(task['age'])
        if len(task['campaign'].split(':')) > 1:
            task['cutcampaign'] = task['campaign'].split(':')[1]
        else:
            task['cutcampaign'] = task['campaign'].split(':')[0]
        if 'reqid' in task and 'jeditaskid' in task and task['reqid'] == task['jeditaskid']:
            task['reqid'] = None
        if 'runnumber' in task:
            task['inputdataset'] = task['runnumber']
        else:
            task['inputdataset'] = None

        if task['inputdataset'] and task['inputdataset'].startswith('00'):
            task['inputdataset'] = task['inputdataset'][2:]
        task['outputtypes'] = ''

        if 'outputdatasettype' in task:
            outputtypes = task['outputdatasettype'].split(',')
        else:
            outputtypes = []
        if len(outputtypes) > 0:
            for outputtype in outputtypes:
                task['outputtypes'] += outputtype.split('_')[1] + ' ' if '_' in outputtype else ''
        if productiontype == 'MC':
            if  task['simtype'] == 'AFII':
                if not task['processingtype'] in neventsAFIItasksSum.keys():
                    neventsAFIItasksSum[str(task['processingtype'])] = 0
                neventsAFIItasksSum[str(task['processingtype'])] += task['nevents'] if task['nevents'] is not None else 0
            elif task['simtype'] == 'FS':
                if not task['processingtype'] in neventsFStasksSum.keys():
                    neventsFStasksSum[str(task['processingtype'])] = 0
                neventsFStasksSum[str(task['processingtype'])] += task['nevents'] if task['nevents'] is not None else 0
        else:
            if not task['processingtype'] in neventsByProcessingType.keys():
                neventsByProcessingType[str(task['processingtype'])] = 0
            neventsByProcessingType[str(task['processingtype'])] += task['nevents'] if task['nevents'] is not None else 0
        if 'hashtags' in task and len(task['hashtags']) > 1:
            task['hashtaglist'] = []
            for hashtag in task['hashtags'].split(','):
                task['hashtaglist'].append(hashtag)

    neventsByStatus = {}
    neventsByStatus['done'] = neventsUsedTotSum
    neventsByStatus['running'] = neventsRunningTotSum
    neventsByStatus['waiting'] = neventsToBeUsedTotSum - neventsRunningTotSum

    plotageshistogram = 1
    if sum(ages) == 0: plotageshistogram = 0
    sumd = taskSummaryDict(request, task_list, ['status','workinggroup','cutcampaign', 'processingtype'])

    ### Putting list of tasks to cache separately for dataTables plugin
    transactionKey = random.randrange(100000000)
    setCacheEntry(request, transactionKey, json.dumps(task_list, cls=DateEncoder), 60 * 30, isData=True)

    if (('HTTP_ACCEPT' in request.META) and (request.META.get('HTTP_ACCEPT') in ('text/json', 'application/json'))) or (
        'json' in request.session['requestParams']):
        ##self monitor
        endSelfMonitor(request)
        if 'snap' in request.session['requestParams'] and len(request.session['requestParams']) == 2:
            snapdata = prepareNeventsByProcessingType(task_list)
            if saveNeventsByProcessingType(snapdata, qtime):
                data = {'message': 'success'}
            else:
                data = {'message': 'fail'}
            dump = json.dumps(data, cls=DateEncoder)
            return HttpResponse(dump, content_type='text/html')
        dump = json.dumps(task_list, cls=DateEncoder)
        return HttpResponse(dump, content_type='text/html')
    else:
        data = {
            'request': request,
            'viewParams': request.session['viewParams'],
            'requestParams': request.session['requestParams'],
            'xurl': xurl,
            'nosorturl': nosorturl,
            'nohashtagurl': nohashtagurl,
            'tasks': task_list,
            'ntasks': ntasks,
            'sortby': sortby,
            'ages': ages,
            'slots': slots,
            'aslots': aslots,
            'aslotsByType' : aslotsByType,
            'sumd': sumd,
            'neventsUsedTotSum': round(neventsUsedTotSum / 1000000., 1),
            'neventsTotSum': round(neventsTotSum / 1000000., 1),
            'neventsWaitingTotSum': round((neventsToBeUsedTotSum - neventsRunningTotSum)/1000000., 1),
            'neventsRunningTotSum': round(neventsRunningTotSum / 1000000., 1),
            'rjobs1coreTot': rjobs1coreTot,
            'rjobs8coreTot': rjobs8coreTot,
            'neventsAFIItasksSum': neventsAFIItasksSum,
            'neventsFStasksSum': neventsFStasksSum,
            'neventsByProcessingType': neventsByProcessingType,
            'neventsByTaskStatus': neventsByTaskStatus,
            'neventsByTaskPriority': neventsByTaskPriority,
            'neventsByStatus' : neventsByStatus,
            'plotageshistogram': plotageshistogram,
            'productiontype' : json.dumps(productiontype),
            'built': datetime.now().strftime("%H:%M:%S"),
            'transKey': transactionKey,
            'qtime': qtime,
        }
        ##self monitor
        endSelfMonitor(request)
        response = render_to_response('runningProdTasks.html', data, content_type='text/html')
        setCacheEntry(request, "runningProdTasks", json.dumps(data, cls=DateEncoder), 60 * 20)
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        return response


@login_customrequired
def runningDPDProdTasks(request):
    return redirect('/runningprodtasks/?preset=DPD')
    valid, response = initRequest(request)

    data = getCacheEntry(request, "runningDPDProdTasks")
    if data is not None:
        data = json.loads(data)
        data['request'] = request
        response = render_to_response('runningDPDProdTasks.html', data, content_type='text/html')
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        endSelfMonitor(request)
        return response


    # xurl = extensibleURL(request)
    xurl = request.get_full_path()
    if xurl.find('?') > 0:
        xurl += '&'
    else:
        xurl += '?'
    nosorturl = removeParam(xurl, 'sortby', mode='extensible')
    tquery = {}
    if 'campaign' in request.session['requestParams']:
        tquery['campaign__contains'] = request.session['requestParams']['campaign']
    if 'corecount' in request.session['requestParams']:
        tquery['corecount'] = request.session['requestParams']['corecount']
    if 'status' in request.session['requestParams']:
        tquery['status'] = request.session['requestParams']['status']
    if 'reqid' in request.session['requestParams']:
        tquery['reqid'] = request.session['requestParams']['reqid']
    if 'inputdataset' in request.session['requestParams']:
        tquery['taskname__contains'] = request.session['requestParams']['inputdataset']
    tasks = RunningDPDProductionTasks.objects.filter(**tquery).values()
    ntasks = len(tasks)
    slots = 0
    ages = []

    neventsTotSum = 0
    neventsUsedTotSum = 0
    rjobs1coreTot = 0
    rjobs8coreTot = 0
    for task in tasks:
        if task['rjobs'] is None:
            task['rjobs'] = 0
        task['neventsused'] = task['totev'] - task['totevrem'] if task['totev'] is not None else 0
        task['percentage'] = round(100. * task['neventsused'] / task['totev'], 1) if task['totev'] > 0 else 0.
        neventsTotSum += task['totev'] if task['totev'] is not None else 0
        neventsUsedTotSum += task['neventsused']
        slots += task['rjobs'] * task['corecount']
        if task['corecount'] == 1:
            rjobs1coreTot += task['rjobs']
        if task['corecount'] == 8:
            rjobs8coreTot += task['rjobs']
        task['age'] = round(
            (datetime.now() - task['creationdate']).days + (datetime.now() - task['creationdate']).seconds / 3600. / 24,
            1)
        ages.append(task['age'])
        if len(task['campaign'].split(':')) > 1:
            task['cutcampaign'] = task['campaign'].split(':')[1]
        else:
            task['cutcampaign'] = task['campaign'].split(':')[0]
        task['inputdataset'] = task['taskname'].split('.')[1]
        if task['inputdataset'].startswith('00'):
            task['inputdataset'] = task['inputdataset'][2:]
        task['tid'] = task['outputtype'].split('_tid')[1].split('_')[0] if '_tid' in task['outputtype'] else None
        task['outputtypes'] = ''
        outputtypes = []
        outputtypes = task['outputtype'].split(',')
        if len(outputtypes) > 0:
            for outputtype in outputtypes:
                task['outputtypes'] += outputtype.split('_')[1].split('_p')[0] + ' ' if '_' in outputtype else ''
        task['ptag'] = task['outputtype'].split('_')[2] if '_' in task['outputtype'] else ''
    plotageshistogram = 1
    if sum(ages) == 0: plotageshistogram = 0
    sumd = taskSummaryDict(request, tasks, ['status'])

    if 'sortby' in request.session['requestParams']:
        sortby = request.session['requestParams']['sortby']
        if sortby == 'campaign-asc':
            tasks = sorted(tasks, key=lambda x: x['campaign'])
        elif sortby == 'campaign-desc':
            tasks = sorted(tasks, key=lambda x: x['campaign'], reverse=True)
        elif sortby == 'reqid-asc':
            tasks = sorted(tasks, key=lambda x: x['reqid'])
        elif sortby == 'reqid-desc':
            tasks = sorted(tasks, key=lambda x: x['reqid'], reverse=True)
        elif sortby == 'jeditaskid-asc':
            tasks = sorted(tasks, key=lambda x: x['jeditaskid'])
        elif sortby == 'jeditaskid-desc':
            tasks = sorted(tasks, key=lambda x: x['jeditaskid'], reverse=True)
        elif sortby == 'rjobs-asc':
            tasks = sorted(tasks, key=lambda x: x['rjobs'])
        elif sortby == 'rjobs-desc':
            tasks = sorted(tasks, key=lambda x: x['rjobs'], reverse=True)
        elif sortby == 'status-asc':
            tasks = sorted(tasks, key=lambda x: x['status'])
        elif sortby == 'status-desc':
            tasks = sorted(tasks, key=lambda x: x['status'], reverse=True)
        elif sortby == 'nevents-asc':
            tasks = sorted(tasks, key=lambda x: x['totev'])
        elif sortby == 'nevents-desc':
            tasks = sorted(tasks, key=lambda x: x['totev'], reverse=True)
        elif sortby == 'neventsused-asc':
            tasks = sorted(tasks, key=lambda x: x['neventsused'])
        elif sortby == 'neventsused-desc':
            tasks = sorted(tasks, key=lambda x: x['neventsused'], reverse=True)
        elif sortby == 'neventstobeused-asc':
            tasks = sorted(tasks, key=lambda x: x['totevrem'])
        elif sortby == 'neventstobeused-desc':
            tasks = sorted(tasks, key=lambda x: x['totevrem'], reverse=True)
        elif sortby == 'percentage-asc':
            tasks = sorted(tasks, key=lambda x: x['percentage'])
        elif sortby == 'percentage-desc':
            tasks = sorted(tasks, key=lambda x: x['percentage'], reverse=True)
        elif sortby == 'nfilesfailed-asc':
            tasks = sorted(tasks, key=lambda x: x['nfilesfailed'])
        elif sortby == 'nfilesfailed-desc':
            tasks = sorted(tasks, key=lambda x: x['nfilesfailed'], reverse=True)
        elif sortby == 'priority-asc':
            tasks = sorted(tasks, key=lambda x: x['currentpriority'])
        elif sortby == 'priority-desc':
            tasks = sorted(tasks, key=lambda x: x['currentpriority'], reverse=True)
        elif sortby == 'ptag-asc':
            tasks = sorted(tasks, key=lambda x: x['ptag'])
        elif sortby == 'ptag-desc':
            tasks = sorted(tasks, key=lambda x: x['ptag'], reverse=True)
        elif sortby == 'outputtype-asc':
            tasks = sorted(tasks, key=lambda x: x['outputtypes'])
        elif sortby == 'output-desc':
            tasks = sorted(tasks, key=lambda x: x['outputtypes'], reverse=True)
        elif sortby == 'age-asc':
            tasks = sorted(tasks, key=lambda x: x['age'])
        elif sortby == 'age-desc':
            tasks = sorted(tasks, key=lambda x: x['age'], reverse=True)
        elif sortby == 'corecount-asc':
            tasks = sorted(tasks, key=lambda x: x['corecount'])
        elif sortby == 'corecount-desc':
            tasks = sorted(tasks, key=lambda x: x['corecount'], reverse=True)
        elif sortby == 'username-asc':
            tasks = sorted(tasks, key=lambda x: x['username'])
        elif sortby == 'username-desc':
            tasks = sorted(tasks, key=lambda x: x['username'], reverse=True)
        elif sortby == 'inputdataset-asc':
            tasks = sorted(tasks, key=lambda x: x['inputdataset'])
        elif sortby == 'inputdataset-desc':
            tasks = sorted(tasks, key=lambda x: x['inputdataset'], reverse=True)
    else:
        sortby = 'age-asc'
        tasks = sorted(tasks, key=lambda x: x['age'])

    if (('HTTP_ACCEPT' in request.META) and (request.META.get('HTTP_ACCEPT') in ('text/json', 'application/json'))) or (
        'json' in request.session['requestParams']):
        ##self monitor
        endSelfMonitor(request)

        dump = json.dumps(tasks, cls=DateEncoder)
        return HttpResponse(dump, content_type='text/html')
    else:
        data = {
            'request': request,
            'viewParams': request.session['viewParams'],
            'requestParams': request.session['requestParams'],
            'xurl': xurl,
            'nosorturl': nosorturl,
            'tasks': tasks,
            'ntasks': ntasks,
            'sortby': sortby,
            'ages': ages,
            'slots': slots,
            'sumd': sumd,
            'neventsUsedTotSum': round(neventsUsedTotSum / 1000000., 1),
            'neventsTotSum': round(neventsTotSum / 1000000., 1),
            'rjobs1coreTot': rjobs1coreTot,
            'rjobs8coreTot': rjobs8coreTot,
            'plotageshistogram': plotageshistogram,
            'built': datetime.now().strftime("%H:%M:%S"),
        }
        ##self monitor
        endSelfMonitor(request)
        response = render_to_response('runningDPDProdTasks.html', data, content_type='text/html')
        setCacheEntry(request, "runningDPDProdTasks", json.dumps(data, cls=DateEncoder), 60 * 20)
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        return response

@login_customrequired
def prodNeventsTrend(request):
    """
    The view presents historical trend of nevents in different states for various processing types
    Default time window - 1 week
    """
    valid, response=  initRequest(request)
    defaultdays = 7
    equery = {}
    if 'days' in request.session['requestParams'] and request.session['requestParams']['days']:
        try:
            days = int(request.session['requestParams']['days'])
        except:
            days = defaultdays
        if days > defaultdays:
            days = defaultdays
        starttime = datetime.now() - timedelta(days=days)
        endtime = datetime.now()
        request.session['requestParams']['days'] = days
    else:
        starttime = datetime.now() - timedelta(days=defaultdays)
        endtime = datetime.now()
        request.session['requestParams']['days'] = defaultdays
    equery['timestamp__range'] = [starttime, endtime]

    if 'processingtype' in request.session['requestParams'] and request.session['requestParams']['processingtype']:
        if '|' not in request.session['requestParams']['processingtype']:
            equery['processingtype'] = request.session['requestParams']['processingtype']
        else:
            pts = request.session['requestParams']['processingtype'].split('|')
            equery['processingtype__in'] = pts

    events = ProdNeventsHistory.objects.filter(**equery).values()

    timeline = set([ev['timestamp'] for ev in events])
    timelinestr = [datetime.strftime(ts, defaultDatetimeFormat) for ts in timeline]

    if 'view' in request.session['requestParams'] and request.session['requestParams']['view'] and request.session['requestParams']['view'] == 'separated':
        view = request.session['requestParams']['view']
    else:
        view = 'joint'

    plot_data = []

    if view == 'joint':
        ev_states = ['running', 'used', 'waiting']

        data = {}
        for es in ev_states:
            data[es] = {}
            for ts in timelinestr:
                data[es][ts] = 0
        for ev in events:
            for es in ev_states:
                data[es][datetime.strftime(ev['timestamp'], defaultDatetimeFormat)] += ev['nevents' + str(es)]

    else:
        processingtypes = set([ev['processingtype'] for ev in events])
        ev_states = ['running', 'waiting']
        lines = []
        for prtype in processingtypes:
            for evst in ev_states:
                lines.append(str(prtype + '_' + evst))
        if len(processingtypes) > 1:
            lines.append('total_running')
            lines.append('total_waiting')

        data = {}
        for l in lines:
            data[l] = {}
            for ts in timelinestr:
                data[l][ts] = 0
        for ev in events:
            for l in lines:
                if ev['processingtype'] in l:
                    data[l][datetime.strftime(ev['timestamp'], defaultDatetimeFormat)] += ev['nevents' + str(l.split('_')[1])]
                if l.startswith('total'):
                    data[l][datetime.strftime(ev['timestamp'], defaultDatetimeFormat)] += ev['nevents' + str(l.split('_')[1])]



    for key, value in data.iteritems():
        newDict = {'state': key, 'values':[]}
        for ts, nevents in value.iteritems():
            newDict['values'].append({'timestamp': ts, 'nevents':nevents})
        newDict['values'] = sorted(newDict['values'], key=lambda k: k['timestamp'])
        plot_data.append(newDict)

    if (('HTTP_ACCEPT' in request.META) and (request.META.get('HTTP_ACCEPT') in ('text/json', 'application/json'))) or (
        'json' in request.session['requestParams']):
        ##self monitor
        endSelfMonitor(request)

        dump = json.dumps(plot_data, cls=DateEncoder)
        return HttpResponse(dump, content_type='text/html')
    else:
        data = {
            'request': request,
            'viewParams': request.session['viewParams'],
            'requestParams': request.session['requestParams'],
            'built': datetime.now().strftime("%H:%M:%S"),
            'plotData': json.dumps(plot_data)
        }

        endSelfMonitor(request)
        response = render_to_response('prodNeventsTrend.html', data, content_type='text/html')
        setCacheEntry(request, "prodNeventsTrend", json.dumps(data, cls=DateEncoder), 60 * 20)
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        return response


@login_customrequired
def runningProdRequests(request):
    valid, response = initRequest(request)

    if ('dt' in request.session['requestParams'] and 'tk' in request.session['requestParams']):
        tk = request.session['requestParams']['tk']
        data = getCacheEntry(request, tk, isData=True)
        return HttpResponse(data, content_type='text/html')

    # Here we try to get cached data
    data = getCacheEntry(request, "runningProdRequests")
    # data = None
    if data is not None:
        data = json.loads(data)
        data['request'] = request
        # if 'ages' in data:
        #     data['ages'] = preparePlotData(data['ages'])
        # if 'neventsFStasksSum' in data:
        #     data['neventsFStasksSum'] = preparePlotData(data['neventsFStasksSum'])
        # if 'neventsAFIItasksSum' in data:
        #     data['neventsAFIItasksSum'] = preparePlotData(data['neventsAFIItasksSum'])
        # if 'neventsByProcessingType' in data:
        #     data['neventsByProcessingType'] = preparePlotData(data['neventsByProcessingType'])
        # if 'aslotsByType' in data:
        #     data['aslotsByType'] = preparePlotData(data['aslotsByType'])
        response = render_to_response('runningProdRequests.html', data, content_type='text/html')
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        endSelfMonitor(request)
        return response



    xurl = request.get_full_path()
    if xurl.find('?') > 0:
        xurl += '&'
    else:
        xurl += '?'
    nosorturl = removeParam(xurl, 'sortby', mode='extensible')
    exquery = {}

    rquery = {}
    if 'fullcampaign' in request.session['requestParams']:
        if ':' in request.session['requestParams']['fullcampaign']:
            rquery['campaign'] = request.session['requestParams']['fullcampaign'].split(':')[0]
            rquery['subcampaign'] = request.session['requestParams']['fullcampaign'].split(':')[1]
        else:
            rquery['campaign'] = request.session['requestParams']['fullcampaign']

    if 'group' in request.session['requestParams'] and '_' in request.session['requestParams']['group']:
        rquery['provenance'] = request.session['requestParams']['group'].split('_')[0]
        rquery['physgroup'] = request.session['requestParams']['group'].split('_')[1]

    if 'requesttype' in request.session['requestParams']:
        rquery['requesttype'] = request.session['requestParams']['requesttype']

    if 'status' in request.session['requestParams']:
        rquery['status'] = request.session['requestParams']['status']


    rrequests = RunningProdRequestsModel.objects.filter(**rquery).values()

    request_list = [t for t in rrequests]
    nrequests = len(request_list)
    slots = 0
    aslots = 0
    # ages = []
    neventsTotSum = 0
    neventsUsedTotSum = 0
    # rjobs1coreTot = 0
    # rjobs8coreTot = 0
    for req in request_list:
        neventsTotSum += req['nevents'] if req['nevents'] is not None else 0
        neventsUsedTotSum += req['neventsused']
        slots += req['slots'] if req['slots'] else 0
        aslots += req['aslots'] if req['aslots'] else 0
        req['fullcampaign'] = req['campaign'] + ':' + req['subcampaign'] if req['subcampaign'] is not None and len(req['subcampaign']) > 0 else req['campaign']
        req['group'] = req['provenance'] + '_' + req['physgroup']

        # ages.append(req['age'])


    plotageshistogram = 0
    # if sum(ages) == 0: plotageshistogram = 0
    # sumd = taskSummaryDict(request, task_list, ['status','workinggroup','cutcampaign', 'processingtype'])

    ### Putting list of requests to cache separately for dataTables plugin
    transactionKey = random.randrange(100000000)
    setCacheEntry(request, transactionKey, json.dumps(request_list, cls=DateEncoder), 60 * 30, isData=True)

    if (('HTTP_ACCEPT' in request.META) and (request.META.get('HTTP_ACCEPT') in ('text/json', 'application/json'))) or (
        'json' in request.session['requestParams']):
        ##self monitor
        endSelfMonitor(request)
        dump = json.dumps(request_list, cls=DateEncoder)
        return HttpResponse(dump, content_type='text/html')
    else:
        data = {
            'request': request,
            'viewParams': request.session['viewParams'],
            'requestParams': request.session['requestParams'],
            'xurl': xurl,
            'nosorturl': nosorturl,
            'requests': request_list,
            'nrequests': nrequests,
            # 'ages': ages,
            'slots': slots,
            'aslots': aslots,
            # 'sumd': sumd,
            'neventsUsedTotSum': round(neventsUsedTotSum / 1000000., 1),
            'neventsTotSum': round(neventsTotSum / 1000000., 1),
            # 'plotageshistogram': plotageshistogram,
            'built': datetime.now().strftime("%H:%M:%S"),
            'transKey': transactionKey,
        }
        ##self monitor
        endSelfMonitor(request)
        response = render_to_response('runningProdRequests.html', data, content_type='text/html')
        setCacheEntry(request, "runningProdRequests", json.dumps(data, cls=DateEncoder), 60 * 20)
        patch_response_headers(response, cache_timeout=request.session['max_age_minutes'] * 60)
        return response
