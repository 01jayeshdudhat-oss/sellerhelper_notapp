import logging
from concurrent.futures import ThreadPoolExecutor
from functools import partial


logger = logging.getLogger(__name__)
_executor = ThreadPoolExecutor(
    max_workers=4,
    thread_name_prefix="sellerhelper-background",
)


def _log_task_failure(task_name, future):
    error = future.exception()
    if error is not None:
        logger.error(
            "Background task %s failed.",
            task_name,
            exc_info=(type(error), error, error.__traceback__),
        )


def submit_background_task(task, *args, task_name):
    future = _executor.submit(task, *args)
    future.add_done_callback(partial(_log_task_failure, task_name))
    return future
