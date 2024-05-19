# Copyright (C) 2015-2021 Regents of the University of California
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
import logging
import math
import os
import shlex
import time
from shlex import quote
from typing import Dict, List, Optional

from toil.batchSystems.abstractGridEngineBatchSystem import \
    AbstractGridEngineBatchSystem
from toil.lib.misc import CalledProcessErrorStderr, call_command

logger = logging.getLogger(__name__)


class GridEngineBatchSystem(AbstractGridEngineBatchSystem):

    class GridEngineThread(AbstractGridEngineBatchSystem.GridEngineThread):
        """
        Grid Engine-specific AbstractGridEngineWorker methods
        """
        def getRunningJobIDs(self):
            times = {}
            with self.runningJobsLock:
                currentjobs = {str(self.batchJobIDs[x][0]): x for x in self.runningJobs}
            stdout = call_command(["qstat"])

            for currline in stdout.split('\n'):
                items = currline.strip().split()
                if items:
                    if items[0] in currentjobs and items[4] == 'r':
                        jobstart = " ".join(items[5:7])
                        jobstart = time.mktime(time.strptime(jobstart, "%m/%d/%Y %H:%M:%S"))
                        times[currentjobs[items[0]]] = time.time() - jobstart

            return times

        def killJob(self, jobID):
            call_command(['qdel', self.getBatchSystemID(jobID)])

        def prepareSubmission(self,
                              cpu: int,
                              memory: int,
                              jobID: int,
                              command: str,
                              jobName: str,
                              job_environment: Optional[Dict[str, str]] = None,
                              gpus: Optional[int] = None):
            # POSIX qsub
            # <https://pubs.opengroup.org/onlinepubs/9699919799.2008edition/utilities/qsub.html>
            # expects a single script argument, which is supposed to be a file.
            # Toil commands usually are not file names but also include
            # arguments. So we split off the arguments like the shell would and
            # hope that the qsub we are using is clever enough to forward along
            # arguments. Otherwise, some qsubs will go looking for the full
            # Toil command string as a file.
            return self.prepareQsub(cpu, memory, jobID, job_environment) + shlex.split(command)

        def submitJob(self, subLine):
            stdout = call_command(subLine)
            output = stdout.split('\n')[0].strip()
            result = int(output)
            return result

        def getJobExitCode(self, sgeJobID):
            """
            Get job exist code, checking both qstat and qacct.  Return None if
            still running.  Higher level should retry on
            CalledProcessErrorStderr, for the case the job has finished and
            qacct result is stale.
            """
            # the task is set as part of the job ID if using getBatchSystemID()
            job, task = (sgeJobID, None)
            if '.' in sgeJobID:
                job, task = sgeJobID.split('.', 1)
            assert task is None, "task ids not currently support by qstat logic below"

            # First try qstat to see if job is still running, if not get the
            # status qacct.  Also, qstat is much faster.
            try:
                call_command(["qstat", "-j", str(job)])
                return None
            except CalledProcessErrorStderr as ex:
                if "Following jobs do not exist" not in ex.stderr:
                    raise

            args = ["qacct", "-j", str(job)]
            if task is not None:
                args.extend(["-t", str(task)])
            stdout = call_command(args)
            for line in stdout.split('\n'):
                if line.startswith("failed") and int(line.split()[1]) == 1:
                    return 1
                elif line.startswith("exit_status"):
                    logger.debug('Exit Status: %r', line.split()[1])
                    return int(line.split()[1])
            return None

        """
        Implementation-specific helper methods
        """
        def prepareQsub(self,
                        cpu: int,
                        mem: int,
                        jobID: int,
                        job_environment: Optional[Dict[str, str]] = None) -> List[str]:
            qsubline = ['qsub', '-V', '-b', 'y', '-terse', '-j', 'y', '-cwd',
                        '-N', 'toil_job_' + str(jobID)]

            environment = self.boss.environment.copy()
            if job_environment:
                environment.update(job_environment)

            if environment:
                qsubline.append('-v')
                qsubline.append(','.join(k + '=' + quote(os.environ[k] if v is None else v)
                                         for k, v in environment.items()))

            reqline = list()
            sgeArgs = os.getenv('TOIL_GRIDENGINE_ARGS')
            if mem is not None:
                memStr = str(mem // 1024) + 'K'
                if not self.boss.config.manualMemArgs:
                    # for UGE instead of SGE; see #2309
                    reqline += ['vf=' + memStr, 'h_vmem=' + memStr]
                elif self.boss.config.manualMemArgs and not sgeArgs:
                    raise ValueError("--manualMemArgs set to True, but TOIL_GRIDGENGINE_ARGS is not set."
                                     "Please set TOIL_GRIDGENGINE_ARGS to specify memory allocation for "
                                     "your system.  Default adds the arguments: vf=<mem> h_vmem=<mem> "
                                     "to qsub.")
            if len(reqline) > 0:
                qsubline.extend(['-hard', '-l', ','.join(reqline)])
            if sgeArgs:
                sgeArgs = sgeArgs.split()
                for arg in sgeArgs:
                    if arg.startswith(("vf=", "h_vmem=", "-pe")):
                        raise ValueError("Unexpected CPU, memory or pe specifications in TOIL_GRIDGENGINE_ARGs: %s" % arg)
                qsubline.extend(sgeArgs)
            # If cpu == 1 (or None) then don't add PE env variable to the qsub command.
            #               This will allow for use of the serial queue for these jobs.
            if (os.getenv('TOIL_GRIDENGINE_PE') is not None) and (cpu is not None) and (cpu > 1) :
                peCpu = int(math.ceil(cpu))
                qsubline.extend(['-pe', os.getenv('TOIL_GRIDENGINE_PE'), str(peCpu)])
            elif (cpu is not None) and (cpu > 1):
                raise RuntimeError("must specify PE in TOIL_GRIDENGINE_PE environment variable when using multiple CPUs. "
                                   "Run qconf -spl and your local documentation for possible values")

            stdoutfile: str = self.boss.format_std_out_err_path(jobID, '$JOB_ID', 'out')
            stderrfile: str = self.boss.format_std_out_err_path(jobID, '$JOB_ID', 'err')
            qsubline.extend(['-o', stdoutfile, '-e', stderrfile])

            return qsubline

    """
    The interface for SGE aka Sun GridEngine.
    """

    @classmethod
    def getWaitDuration(cls):
        return 1
