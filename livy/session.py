import json
import logging
import asyncio

import pandas

from livy.models import SessionKind, SessionState, StatementState
from livy.client import LivyClient


LOGGER = logging.getLogger(__name__)

SERIALISE_DATAFRAME_TEMPLATE_SPARK = '{}.toJSON.collect.foreach(println)'
SERIALISE_DATAFRAME_TEMPLATE_PYSPARK = """
for _livy_client_serialised_row in {}.toJSON().collect():
    print(_livy_client_serialised_row)
"""
SERIALISE_DATAFRAME_TEMPLATE_SPARKR = r"""
cat(unlist(collect(toJSON({}))), sep = '\n')
"""


def serialise_dataframe_code(dataframe_name, session_kind):
    try:
        template = {
            SessionKind.SPARK: SERIALISE_DATAFRAME_TEMPLATE_SPARK,
            SessionKind.PYSPARK: SERIALISE_DATAFRAME_TEMPLATE_PYSPARK,
            SessionKind.SPARKR: SERIALISE_DATAFRAME_TEMPLATE_SPARKR
        }[session_kind]
    except KeyError:
        raise RuntimeError(
            f'read not supported for sessions of kind {session_kind}'
        )
    return template.format(dataframe_name)


def run_sync(coroutine):
    loop = asyncio.get_event_loop()
    return loop.run_until_complete(asyncio.ensure_future(coroutine))


def deserialise_dataframe(text):
    rows = []
    for line in text.split('\n'):
        if line:
            rows.append(json.loads(line))
    return pandas.DataFrame(rows)


async def wait_until_statement_finished(client, session_id, statement_id,
                                        interval=1.0):

    async def finished():
        statement = await client.get_statement(session_id, statement_id)
        return statement.state not in {StatementState.WAITING,
                                       StatementState.RUNNING}

    while not await finished():
        await asyncio.sleep(interval)


class BaseLivySession:

    def __init__(self, url, kind=SessionKind.PYSPARK, echo=True, check=True):
        self.client = LivyClient(url)
        self.kind = kind
        self.session_id = None
        self.echo = echo
        self.check = check

    async def _start(self):
        session = await self.client.create_session(self.kind)
        self.session_id = session.session_id

        not_ready = {SessionState.NOT_STARTED, SessionState.STARTING}

        while (await self._state()) in not_ready:
            await asyncio.sleep(1.0)

    async def _state(self):
        if self.session_id is None:
            raise ValueError('session not yet started')
        session = await self.client.get_session(self.session_id)
        if session is None:
            raise ValueError('session not found - it may have been shut down')
        return session.state

    async def _close(self):
        await self.client.delete_session(self.session_id)
        await self.client.close()

    async def _execute(self, code):
        LOGGER.info('Beginning code statement execution')
        statement = await self.client.create_statement(self.session_id, code)
        await wait_until_statement_finished(
            self.client, statement.session_id, statement.statement_id
        )
        statement = await self.client.get_statement(
            statement.session_id, statement.statement_id
        )
        LOGGER.info(
            'Completed code statement execution with status '
            f'{statement.output.status}'
        )
        return statement.output


class LivySession(BaseLivySession):

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def start(self):
        run_sync(self._start())

    def close(self):
        run_sync(self._close())

    @property
    def state(self):
        return run_sync(self._state())

    def run(self, code):
        output = run_sync(self._execute(code))
        if self.echo and output.text:
            print(output.text)
        if self.check:
            output.raise_for_status()
        return output

    def read(self, dataframe_name):
        code = serialise_dataframe_code(dataframe_name, self.kind)
        output = run_sync(self._execute(code))
        output.raise_for_status()
        return deserialise_dataframe(output.text)


class AsyncLivySession(BaseLivySession):

    async def __aenter__(self):
        await self.start()
        return self

    async def __aexit__(self, exc_type, exc_value, traceback):
        await self.close()

    async def start(self):
        await self._start()

    async def close(self):
        await self._close()

    @property
    async def state(self):
        return await self._state()

    async def run(self, code):
        output = await self._execute(code)
        if self.echo and output.text:
            print(output.text)
        if self.check:
            output.raise_for_status()
        return output

    async def read(self, dataframe_name):
        code = serialise_dataframe_code(dataframe_name, self.kind)
        output = await self._execute(code)
        output.raise_for_status()
        return deserialise_dataframe(output.text)