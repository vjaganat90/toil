import errno
import multiprocessing
import os
from functools import partial

from toil.lib.io import mkdtemp
from toil.lib.threading import cpu_count
from toil.test import ToilTest


class SystemTest(ToilTest):
    """Test various assumptions about the operating system's behavior."""

    def testAtomicityOfNonEmptyDirectoryRenames(self):
        for _ in range(100):
            parent = self._createTempDir(purpose="parent")
            child = os.path.join(parent, "child")
            # Use processes (as opposed to threads) to prevent GIL from ordering things artificially
            pool = multiprocessing.Pool(processes=cpu_count())
            try:
                numTasks = cpu_count() * 10
                grandChildIds = pool.map_async(
                    func=partial(
                        _testAtomicityOfNonEmptyDirectoryRenamesTask, parent, child
                    ),
                    iterable=list(range(numTasks)),
                )
                grandChildIds = grandChildIds.get()
            finally:
                pool.close()
                pool.join()
            self.assertEqual(len(grandChildIds), numTasks)
            # Assert that we only had one winner
            grandChildIds = [n for n in grandChildIds if n is not None]
            self.assertEqual(len(grandChildIds), 1)
            # Assert that the winner's grandChild wasn't silently overwritten by a looser
            expectedGrandChildId = grandChildIds[0]
            actualGrandChild = os.path.join(child, "grandChild")
            actualGrandChildId = os.stat(actualGrandChild).st_ino
            self.assertEqual(actualGrandChildId, expectedGrandChildId)


def _testAtomicityOfNonEmptyDirectoryRenamesTask(parent, child, _):
    tmpChildDir = mkdtemp(dir=parent, prefix="child", suffix=".tmp")
    grandChild = os.path.join(tmpChildDir, "grandChild")
    open(grandChild, "w").close()
    grandChildId = os.stat(grandChild).st_ino
    try:
        os.rename(tmpChildDir, child)
    except OSError as e:
        if e.errno == errno.ENOTEMPTY or e.errno == errno.EEXIST:
            os.unlink(grandChild)
            os.rmdir(tmpChildDir)
            return None
        else:
            raise
    else:
        # We won the race
        return grandChildId
