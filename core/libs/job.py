"""
Set of functions related to jobs metadata

Created by Tatiana Korchuganova on 05.03.2020
"""

from datetime import datetime, timedelta
from core.pandajob.models import Jobsactive4, Jobsarchived, Jobswaiting4, Jobsdefined4, Jobsarchived4
from core.libs.exlib import get_tmp_table_name, insert_to_temp_table, get_job_walltime, get_job_queuetime


def is_event_service(job):
    if 'eventservice' in job and job['eventservice'] is not None:
        if 'specialhandling' in job and job['specialhandling'] and (
                    job['specialhandling'].find('eventservice') >= 0 or job['specialhandling'].find('esmerge') >= 0 or (
                job['eventservice'] != 'ordinary' and job['eventservice'])) and job['specialhandling'].find('sc:') == -1:
                return True
        else:
            return False
    else:
        return False


def get_job_list(query, **kwargs):

    MAX_ENTRIES__IN = 100

    jobs = []
    values = [
        'actualcorecount', 'eventservice', 'specialhandling', 'modificationtime', 'jobsubstatus', 'pandaid',
        'jobstatus', 'jeditaskid', 'processingtype', 'maxpss', 'starttime', 'endtime', 'computingsite',
        'jobsetid', 'jobmetrics', 'nevents', 'hs06', 'hs06sec', 'cpuconsumptiontime', 'parentid', 'attemptnr',
        'processingtype', 'transformation', 'creationtime'
    ]
    if 'values' in kwargs:
        values.extend(kwargs['values'])
        values = set(values)

    extra_str = "(1=1)"
    if 'extra_str' in kwargs and kwargs['extra_str'] != '':
        extra_str = kwargs['extra_str']

    if 'jeditaskid__in' in query and len(query['jeditaskid__in']) > MAX_ENTRIES__IN:
        # insert taskids to temp DB table
        tmp_table_name = get_tmp_table_name()
        tk_taskids = insert_to_temp_table(query['jeditaskid__in'])
        extra_str += " AND jeditaskid in (select ID from {} where TRANSACTIONKEY={})".format(tmp_table_name, tk_taskids)
        del query['jeditaskid__in']

    jobs.extend(Jobsdefined4.objects.filter(**query).extra(where=[extra_str]).values(*values))
    jobs.extend(Jobswaiting4.objects.filter(**query).extra(where=[extra_str]).values(*values))
    jobs.extend(Jobsactive4.objects.filter(**query).extra(where=[extra_str]).values(*values))
    jobs.extend(Jobsarchived4.objects.filter(**query).extra(where=[extra_str]).values(*values))

    if 'jeditaskid' in query or 'jeditaskid__in' in query or 'jeditaskid' in extra_str or (
        'modificationtime__castdate__range' in query and query['modificationtime__castdate__range'][0] < (
            datetime.now() - timedelta(days=3))):
        jobs.extend(Jobsarchived.objects.filter(**query).extra(where=[extra_str]).values(*values))

    return jobs


def calc_jobs_metrics(jobs, group_by='jeditaskid'):
    """
    Calculate interesting metrics, e.g. avg maxpss/core, avg job walltime, avg job queuetime, failedpct
    :param jobs: list of dicts
    :param group_by:
    :return metrics_dict: dict
    """
    metrics_dict = {
        'maxpss_per_actualcorecount': {'total': [], 'group_by': {}},
        'walltime': {'total': [], 'group_by': {}},
        'queuetime': {'total': [], 'group_by': {}},
        'failed': {'total': [], 'group_by': {}},
    }

    # calc metrics
    for job in jobs:
        if group_by in job and job[group_by]:

            if 'maxpss' in job and job['maxpss'] and isinstance(job['maxpss'], int) and (
                    'actualcorecount' in job and isinstance(job['actualcorecount'], int) and job['actualcorecount'] > 0):
                job['maxpss_per_actualcorecount'] = 1.0 * job['maxpss'] / job['actualcorecount'] / 1024.

            job['walltime'] = get_job_walltime(job)
            job['walltime'] = round(job['walltime'] / 60. / 60., 2) if job['walltime'] is not None else job['walltime']
            job['queuetime'] = get_job_queuetime(job)
            job['queuetime'] = round(job['queuetime'] / 60. / 60., 2) if job['queuetime'] is not None else job['queuetime']

            job['failed'] = 100 if 'jobstatus' in job and job['jobstatus'] == 'failed' else 0

            for metric in metrics_dict:
                if metric in job and job[metric] is not None:
                    if job[group_by] not in metrics_dict[metric]['group_by']:
                        metrics_dict[metric]['group_by'][job[group_by]] = []
                    metrics_dict[metric]['group_by'][job[group_by]].append(job[metric])
                    metrics_dict[metric]['total'].append(job[metric])

    for metric in metrics_dict:
        if len(metrics_dict[metric]['total']) > 0:
            metrics_dict[metric]['total'] = round(
                sum(metrics_dict[metric]['total'])/len(metrics_dict[metric]['total']), 2)
        else:
            metrics_dict[metric]['total'] = -1
        for gbp in metrics_dict[metric]['group_by']:
            if len(metrics_dict[metric]['group_by'][gbp]) > 0:
                metrics_dict[metric]['group_by'][gbp] = round(
                    sum(metrics_dict[metric]['group_by'][gbp]) / len(metrics_dict[metric]['group_by'][gbp]), 2)
            else:
                metrics_dict[metric]['group_by'][gbp] = -1


    return metrics_dict
