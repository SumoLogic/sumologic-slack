import os
import sys
import traceback
from random import shuffle

from concurrent import futures
from slackclient import SlackClient
from sumoappclient.common.utils import get_current_timestamp
from sumoappclient.sumoclient.base import BaseCollector

from api import UsersDataAPI, ChannelsMessagesAPI, AccessLogsAPI, AuditLogsAPI, ChannelsDataAPI


def get_current_dir():
    cur_dir = os.path.dirname(__file__)
    return cur_dir


class SumoSlackCollector(BaseCollector):
    SINGLE_PROCESS_LOCK_KEY = 'is_slack_collector_running'
    CONFIG_FILENAME = "slackcollector.yaml"
    CHANNEL_COUNTER = 50
    MAX_PAGE = 101
    PAGE_COUNTER = 15

    def __init__(self):
        self.project_dir = get_current_dir()
        super(SumoSlackCollector, self).__init__(self.project_dir)

        # Set Slack configuration and create Slack Client
        self.api_config = self.config['Slack']
        self.token = self.config['Slack']['TOKEN']
        self.slackClient = SlackClient(self.token)

        # Populate Team Name in Key Value Pair
        self.team_name = self._set_team_name()

    def _set_team_name(self):
        data = self.slackClient.api_call("team.info", self.collection_config['TIMEOUT'])
        if data is not None and data["ok"] and "team" in data:
            self.kvstore.set(data["team"]["id"], data["team"]["name"])
            return data["team"]["name"]
        else:
            self.log.error("Team name call failed with error as %s", data["error"])
            sys.exit()

    def is_running(self):
        self.log.debug("Acquiring single instance lock")
        return self.kvstore.acquire_lock(self.SINGLE_PROCESS_LOCK_KEY)

    def stop_running(self):
        self.log.debug("Releasing single instance lock")
        return self.kvstore.release_lock(self.SINGLE_PROCESS_LOCK_KEY)

    def build_task_params(self):
        tasks = []
        if 'LOG_TYPES' in self.api_config:
            if "USER_LOGS" in self.api_config['LOG_TYPES']:
                tasks.append(UsersDataAPI(self.kvstore, self.config, self.team_name))

            if "CHANNELS_LOGS" in self.api_config['LOG_TYPES']:
                # fetch the state again to check if the channels ID are changed
                obj = self._get_channel_ids()
                if obj is not None and "ids" in obj:
                    channels_ids = obj["ids"]
                    counter = self.kvstore.get("channel_id_index", 0)
                    next_counter = counter + self.CHANNEL_COUNTER
                    channel_batch = channels_ids[counter: next_counter]

                    for channels_id in channel_batch:
                        channel = channels_id.split("#")
                        tasks.append(
                            ChannelsMessagesAPI(self.kvstore, self.config, channel[0], channel[1], self.team_name))

                    self.kvstore.set("channel_id_index", next_counter)
                    if len(channels_ids) <= next_counter:
                        self.log.debug("Resetting the channel counter")
                        self.kvstore.set("channel_id_index", 0)

            if "ACCESS_LOGS" in self.api_config['LOG_TYPES']:
                page = self.kvstore.get("Access_logs_page_index", 1)
                next_page = min(self.MAX_PAGE, page + self.PAGE_COUNTER)
                for page_number in range(page, next_page):
                    tasks.append(AccessLogsAPI(self.kvstore, self.config, page_number, self.team_name))

                self.kvstore.set("Access_logs_page_index", next_page)
                if next_page == self.MAX_PAGE:
                    self.kvstore.set("Access_logs_page_index", 1)
                    self.kvstore.delete("fetch_before")

            if "AUDIT_LOGS" in self.api_config['LOG_TYPES'] and "AUDIT_LOG_URL" in self.api_config:
                tasks.append(AuditLogsAPI(self.kvstore, self.config, self.api_config["AUDIT_LOG_URL"], self.team_name))
        return tasks

    def _get_channel_ids(self):
        channels_data = ChannelsDataAPI(self.kvstore, self.config, self.team_name)
        obj = channels_data.get_state()
        if obj is None or ("ids" in obj and len(obj["ids"]) <= 0) or ("last_fetched" in obj and not (
                get_current_timestamp() - obj["last_fetched"] < channels_data.DATA_REFRESH_TIME)):
            channels_data.fetch()
        else:
            self.log.info("Channels Data will not be fetched as 6 Data refresh time of 6 Hours not reached")
        return channels_data.get_state()

    def run(self, *args, **kwargs):
        if self.is_running():
            try:
                self.log.info('Starting MongoDB Atlas Forwarder...')
                task_params = self.build_task_params()
                shuffle(task_params)
                all_futures = {}
                self.log.debug("spawning %d workers" % self.config['Collection']['NUM_WORKERS'])
                with futures.ThreadPoolExecutor(max_workers=self.config['Collection']['NUM_WORKERS']) as executor:
                    results = {executor.submit(apiobj.fetch): apiobj for apiobj in task_params}
                    all_futures.update(results)
                for future in futures.as_completed(all_futures):
                    param = all_futures[future]
                    api_type = str(param)
                    try:
                        future.result()
                        obj = self.kvstore.get(api_type)
                    except Exception as exc:
                        self.log.error(f"API Type: {api_type} thread generated an exception: {exc}", exc_info=True)
                    else:
                        self.log.info(f"API Type: {api_type} thread completed {obj}")
            finally:
                self.stop_running()
        else:
            self.kvstore.release_lock_on_expired_key(self.SINGLE_PROCESS_LOCK_KEY, expiry_min=10)


def main(*args, **kwargs):
    try:
        ns = SumoSlackCollector()
        ns.run()
        # ns.test()
    except BaseException as e:
        traceback.print_exc()


if __name__ == '__main__':
    main()
