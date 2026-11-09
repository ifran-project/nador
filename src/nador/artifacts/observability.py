"""Native experiment context collection, with no cluster operations on import."""

from __future__ import annotations

import asyncio
import json
import math
from pathlib import Path
import shlex
import threading
from types import SimpleNamespace
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from . import ue_context
from .run import ExperimentRun
from .connection import loki_connection
from contextlib import AsyncExitStack

if TYPE_CHECKING:
    from nador.deployment import MonitoringManagement


class ObservabilityRun(ExperimentRun):
    """Collect setup before/after a workload and bounded sampled UE inventory.

    A stage enables remote host/cluster snapshots and service-proxy sampling.
    A mapper_url instead samples directly from this process. Sampling observes
    inventory, not every UE transition. Collection errors follow ExperimentRun's
    policy: fail successful workloads, but never replace a workload exception.
    """

    def __init__(self, directory, *, monitoring: MonitoringManagement | None = None,
                 mapper_url=None, collect_setup=True, collect_ue_context=True, setup_hosts=None,
                 interval_seconds=2, timeout_seconds=5, max_seconds=7200,
                 limit=5000, namespace=None, service='ue-mapper-api', service_port=8080,
                 loki_url=None, collect_loki=False, loki_selector='{namespace=~".+"}', loki_limit=5000, **kwargs):
        kwargs.setdefault('queries_file', Path(__file__).resolve().parents[1]
                          / 'assets' / 'observability_queries.json')
        super().__init__(directory, monitoring=monitoring, **kwargs)
        if (isinstance(interval_seconds, bool) or not math.isfinite(interval_seconds)
                or not .1 <= interval_seconds <= 3600):
            raise ValueError('interval_seconds must be finite and between 0.1 and 3600')
        for name, value in [('timeout_seconds', timeout_seconds), ('max_seconds', max_seconds), ('limit', limit)]:
            if type(value) is not int or value < 1:
                raise ValueError(f'{name} must be a positive integer')
        if timeout_seconds > 60:
            raise ValueError('timeout_seconds must not exceed 60')
        if type(service_port) is not int or not 1 <= service_port <= 65535:
            raise ValueError('service_port must be an integer between 1 and 65535')
        if type(loki_limit) is not int or not 1 <= loki_limit <= 100000:
            raise ValueError('loki_limit must be between 1 and 100000')
        if mapper_url:
            parsed = urlsplit(mapper_url)
            if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.query or parsed.fragment:
                raise ValueError('mapper_url must be an HTTP(S) base URL without query or fragment')
        self.stage = monitoring
        self.mapper_url = mapper_url
        self.loki_url = loki_url
        self.collect_loki = collect_loki or bool(loki_url)
        self.loki_selector = loki_selector
        self.loki_limit = loki_limit
        self.collect_setup = collect_setup
        self.collect_ue_context = collect_ue_context
        if setup_hosts is not None and (not isinstance(setup_hosts, list)
                or any(not isinstance(host, str) or not host for host in setup_hosts)):
            raise ValueError('setup_hosts must be a list of node names')
        if setup_hosts and (monitoring is None or any(host not in monitoring.deployment_model.nodes for host in setup_hosts)):
            raise ValueError('setup_hosts must refer to nodes in the supplied stage')
        self.setup_hosts = setup_hosts
        self.options = dict(interval_seconds=interval_seconds, timeout_seconds=timeout_seconds,
                            max_seconds=max_seconds, limit=limit, service=service, service_port=service_port)
        self.namespace = namespace
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._thread = None
        self._thread_error = None
        self._remote_directory = None
        self._job_id = None
        self._remote_config = None

    async def _finalize(self, ended):
        async with AsyncExitStack() as access:
            if self.collect_loki and not self.loki_url and self.monitoring is not None:
                try:
                    self.loki_url = await access.enter_async_context(loki_connection(self.monitoring))
                except Exception as exc:
                    self._error('loki access', exc)
            await super()._finalize(ended)

    def _collect(self, ended):
        super()._collect(ended)
        if self.loki_url:
            try:
                from .loki_export import collect_logs

                summary = collect_logs(self.directory / 'logs', url=self.loki_url,
                    selector=self.loki_selector, start=self.started, end=ended, limit=self.loki_limit)
                if summary.get('error'):
                    self._error('loki', summary['error'])
                if summary.get('possible_truncation'):
                    self._error('loki', 'possible truncation; log export may be incomplete')
            except Exception as exc:
                self._error('loki', exc)

    def _error(self, label, exc):
        self.metadata['collection_errors'].append(f'{label}: {exc}')

    def _write(self, path, data):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + '\n', encoding='utf-8')

    def _control(self):
        if self._remote_config is None:
            model = self.stage.deployment_model
            cfg = model.monitoring
            name = model.params.k8s.master_node
            if not name or name not in model.nodes:
                raise ValueError('ML context requires a control node in stage.deployment_model.nodes')
            self._remote_config = (name, cfg.kubeconfig, self.namespace or cfg.namespace)
        return self._remote_config

    async def _run(self, tasks, *, host=None, output_keys=None):
        # Keep Ansible (and its model dependencies) optional for direct HTTP runs.
        from orchestrator.utils.ansible import get_ansible_inventory_line, run_ansible_playbook

        model = self.stage.deployment_model
        host = host or self._control()[0]
        inventory = '[ml_context]\n' + get_ansible_inventory_line(host, model.nodes[host]) + '\n'
        return await run_ansible_playbook(
            playbook=[{'name': 'ML experiment context', 'hosts': 'ml_context',
                       'gather_facts': False, 'become': True, 'tasks': tasks}],
            inventory=inventory, output_keys=output_keys, quiet=True,
            runner_path=str(Path(model.params.ansible.runner_path) / 'ml-artifacts' / self.run_id))

    async def _snapshot(self, phase):
        if not self.stage or not self.collect_setup:
            return
        output = self.directory / 'setup' / phase
        script = Path(__file__).with_name('collect_setup.py')
        hosts = []
        nodes = self.stage.deployment_model.nodes
        selected = self.setup_hosts
        if selected is None:
            control = self._control()[0]
            selected = [name for name, node in nodes.items()
                        if name == control or getattr(getattr(node, 'ssh_info', None), 'hostname', None)]
        for host in selected:
            try:
                snapshot = await self._read_snapshot(script, ['--mode', 'host'], host)
                hosts.append({'host': host, **snapshot})
            except Exception as exc:
                self._error(f'setup {phase} host {host}', exc)
                hosts.append({'host': host, 'status': 'error', 'error': str(exc)})
        self._write(output / 'hosts.json', hosts)
        try:
            host, kubeconfig, _ = self._control()
            snapshot = await self._read_snapshot(script, ['--mode', 'cluster', '--kubeconfig', kubeconfig], host)
            self._write(output / 'cluster.json', snapshot)
        except Exception as exc:
            self._error(f'setup {phase} cluster', exc)
            self._write(output / 'cluster.json', {'status': 'error', 'error': str(exc)})

    async def _read_snapshot(self, script, args, host):
        result = await self._run([
            {'name': 'Read setup snapshot', 'ansible.builtin.script': {
                'cmd': shlex.join([str(script), *args]), 'executable': 'python3'},
             'register': 'snapshot', 'changed_when': False},
            {'ansible.builtin.set_stats': {'data': {'snapshot': '{{ snapshot.stdout }}'}}},
        ], host=host, output_keys=['snapshot'])
        snapshot = json.loads(result['snapshot'])
        if not isinstance(snapshot, dict) or not isinstance(snapshot.get('data'), dict):
            raise ValueError('Invalid setup snapshot')
        for name, value in snapshot['data'].items():
            if isinstance(value, dict) and value.get('status') == 'error':
                self._error(f'setup {host} {name}', value.get('error', 'command failed'))
        return snapshot

    def _sample_direct(self, args):
        try:
            ue_context.sample(args, lambda: ue_context.fetch_inventory(args, []), self._stop, self._ready)
        except Exception as exc:
            self._thread_error = exc
        finally:
            self._ready.set()

    async def _start_sampler(self):
        if not self.collect_ue_context or not (self.mapper_url or self.stage):
            return
        if self.mapper_url:
            args = SimpleNamespace(**self.options, url=self.mapper_url,
                out_dir=str(self.directory / 'ue_context'),
                stop_file=str(self.directory / 'ue_context' / 'STOP'), run_id=self.run_id)
            self._thread = threading.Thread(target=self._sample_direct, args=(args,),
                                            name=f'ue-context-{self.run_id}')
            self._thread.start()
            await asyncio.to_thread(self._ready.wait)
            if self._thread_error:
                raise self._thread_error
            return
        _, kubeconfig, namespace = self._control()
        self._remote_directory = '/tmp/orchestrator-ml-' + self.run_id
        remote = self._remote_directory
        argv = ['python3', remote + '/probe.py', '--out-dir', remote,
                '--stop-file', remote + '/STOP', '--run-id', self.run_id,
                '--namespace', namespace, '--kubeconfig', kubeconfig]
        for key, value in self.options.items():
            argv += ['--' + key.replace('_', '-'), str(value)]
        result = await self._run([
            {'ansible.builtin.file': {'path': remote, 'state': 'directory', 'mode': '0700'}},
            {'ansible.builtin.copy': {'src': str(Path(__file__).with_name('ue_context.py')),
                                     'dest': remote + '/probe.py', 'mode': '0700'}},
            {'name': 'Start bounded UE context sampling', 'ansible.builtin.command': {'argv': argv},
             'async': self.options['max_seconds'] + self.options['timeout_seconds'] + 30,
             'poll': 0, 'register': 'sampler'},
            {'ansible.builtin.set_stats': {'data': {'job_id': '{{ sampler.ansible_job_id }}'}}},
        ], output_keys=['job_id'])
        self._job_id = result['job_id']
        await self._run([{'ansible.builtin.wait_for': {
            'path': remote + '/ready.json', 'timeout': self.options['timeout_seconds'] + 15}}])

    async def _stop_sampler(self):
        output = self.directory / 'ue_context'
        status = {'mode': 'direct' if self.mapper_url else 'remote', 'remote_directory': self._remote_directory}
        try:
            if self._thread:
                self._stop.set()
                await asyncio.to_thread(self._thread.join)
                if self._thread_error:
                    raise self._thread_error
            elif self._remote_directory:
                remote = self._remote_directory
                wait = [{'ansible.builtin.copy': {'dest': remote + '/STOP', 'content': 'stop\n', 'mode': '0600'}},
                        {'ansible.builtin.wait_for': {'path': remote + '/summary.json',
                         'timeout': 2 * self.options['timeout_seconds'] + 15}}]
                if self._job_id:
                    wait.append({'ansible.builtin.async_status': {'jid': self._job_id},
                                 'register': 'sampler_exit', 'until': 'sampler_exit.finished | default(false)',
                                 'retries': self.options['timeout_seconds'] + 15, 'delay': 1})
                output.mkdir(parents=True, exist_ok=True)
                # Always try to retrieve partial evidence, even if stopping failed.
                await self._run([{'block': wait, 'always': [
                    {'ansible.builtin.fetch': {'src': remote + '/{{ item }}',
                     'dest': str(output.resolve()) + '/', 'flat': True},
                     'loop': ['history.jsonl', 'summary.json', 'ready.json']},
                ]}])
                cleanup = []
                if self._job_id:
                    cleanup.append({'ansible.builtin.async_status': {'jid': self._job_id, 'mode': 'cleanup'}})
                cleanup.append({'ansible.builtin.file': {'path': remote, 'state': 'absent'}})
                await self._run(cleanup)
            else:
                return
            summary = json.loads((output / 'summary.json').read_text())
            if summary.get('run_id') != self.run_id or not summary.get('samples'):
                raise ValueError('Missing or mismatched UE context samples')
            if summary.get('errors'):
                self._error('ue_context', f"{summary['errors']} failed inventory polls")
            if summary.get('stop_reason') == 'max_seconds':
                self._error('ue_context', 'max_seconds reached; history may not cover the full workload')
            status['status'] = 'completed'
        except Exception as exc:
            status.update(status='error', error=str(exc))
            self._error('ue_context stop/fetch', exc)
        finally:
            if self._thread or self._remote_directory:
                self._write(output / 'collection_status.json', status)

    async def __aenter__(self):
        await super().__aenter__()
        async def start():
            for label, operation in [('setup before', lambda: self._snapshot('before')),
                                     ('ue_context start', self._start_sampler)]:
                try:
                    await operation()
                except Exception as exc:
                    self._error(label, exc)
        task = asyncio.create_task(start())
        try:
            await asyncio.shield(task)
        except BaseException as exc:
            # Finish startup before stopping: otherwise an async remote launch can
            # race cleanup and leave an untracked sampler running.
            await self._drain(task)
            await self.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        return self

    @staticmethod
    async def _drain(task):
        cancelled = None
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError as exc:
                cancelled = exc
            except Exception:
                break
        return cancelled

    async def __aexit__(self, exc_type, exc, tb):
        async def finish():
            for label, operation in [('ue_context cleanup', self._stop_sampler),
                                     ('setup after', lambda: self._snapshot('after'))]:
                try:
                    await operation()
                except Exception as error:
                    self._error(label, error)
            return await super(ObservabilityRun, self).__aexit__(exc_type, exc, tb)
        task = asyncio.create_task(finish())
        cancelled = await self._drain(task)
        if cancelled:
            # A collection failure must not mask cancellation either.
            if not task.cancelled():
                task.exception()
            self.metadata['status'] = 'cancelled'
            self._save_metadata()
            raise cancelled
        return task.result()
