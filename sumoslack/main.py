import os
import sys
import traceback
from random import shuffle

sys.path.insert(0, '/opt')  # layer packages are in opt directory

from concurrent import futures
from slackclient import SlackClient
from sumoappclient.common.utils import get_current_timestamp
from sumoappclient.sumoclient.base import BaseCollector
from sumoappclient.sumoclient.httputils import ClientMixin

from api import UsersDataAPI, ChannelsMessagesAPI, AccessLogsAPI, AuditLogsAPI, ChannelsDataAPI


def get_current_dir():
    cur_dir = os.path.dirname(__file__)
    return cur_dir


class SumoSlackCollector(BaseCollector):
    SINGLE_PROCESS_LOCK_KEY = 'is_slack_collector_running'
    CONFIG_FILENAME = "slackcollector.yaml"
    CHANNEL_COUNTER = 50
    MAX_PAGE = 101
    PAGE_COUNTER = 2
    DATA_REFRESH_TIME = 24 * 60 * 60

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
        shuffle_tasks = []
        if 'LOG_TYPES' in self.api_config:
            if "USER_LOGS" in self.api_config['LOG_TYPES']:
                tasks.append(UsersDataAPI(self.kvstore, self.config, self.team_name))

            next_counter = self.kvstore.get("channel_id_index", 0) + 1
            channels = self._get_channel_ids(next_counter)

            if "CHANNELS_MESSAGES_LOGS" in self.api_config['LOG_TYPES']:
                # fetch the state again to check if the channels ID are changed

                if channels is not None and "ids" in channels:
                    channels_ids = channels["ids"]

                    for channels_id in channels_ids:
                        channel = channels_id.split("#")
                        shuffle_tasks.append(
                            ChannelsMessagesAPI(self.kvstore, self.config, channel[0], channel[1], self.team_name))

                    self.kvstore.set("channel_id_index", next_counter)

            call_access_logs = True

            if self.kvstore.get("Access_logs_page_index") == 1 \
                    and get_current_timestamp() - self.kvstore.get("Access_logs_call_time") < self.DATA_REFRESH_TIME:
                self.log.info("All Data for access logs has been sent. New Data will be sent after every 24 Hours.")
                call_access_logs = False

            if "ACCESS_LOGS" in self.api_config['LOG_TYPES'] and call_access_logs:
                page = self.kvstore.get("Access_logs_page_index", 1)
                next_page = min(self.MAX_PAGE, page + self.PAGE_COUNTER)
                for page_number in range(page, next_page):
                    tasks.append(AccessLogsAPI(self.kvstore, self.config, page_number, self.team_name))

                self.kvstore.set("Access_logs_page_index", next_page)
                if page == 1:
                    self.kvstore.set("Access_logs_call_time", get_current_timestamp())

                if next_page == self.MAX_PAGE:
                    self.kvstore.set("Access_logs_page_index", 1)
                    self.kvstore.delete("AccessLogs")

            if "AUDIT_LOGS" in self.api_config['LOG_TYPES'] and "AUDIT_LOG_URL" in self.api_config:
                self._get_audit_actions(self.api_config["AUDIT_LOG_URL"])
                shuffle_tasks.append(
                    AuditLogsAPI(self.kvstore, self.config, self.api_config["AUDIT_LOG_URL"], self.team_name,
                                 self.WorkspaceAuditActions, self.UserAuditActions, self.ChannelAuditActions,
                                 self.FileAuditActions, self.AppAuditActions, self.OtherAuditActions))

        shuffle(shuffle_tasks)
        tasks.extend(shuffle_tasks)
        return tasks

    def _get_channel_ids(self, counter):
        channels_data = ChannelsDataAPI(self.kvstore, self.config, self.team_name, counter)
        obj = channels_data.get_state()
        if obj is None or ("ids" in obj and len(obj["ids"]) <= 0
                           or (counter > self.kvstore.get("Channels_Page_Number"))):
            if "CHANNELS_LOGS" in self.api_config['LOG_TYPES']:
                self.kvstore.delete("Channels_Page_Number")
                channels_data.fetch()
                self.kvstore.delete("channel_id_index")
                return None
        else:
            self.log.info("Channels Data will not be fetched as all channels message data is not sent.")
        return obj

    def _get_audit_actions(self, audit_url):
        url = audit_url + "actions"
        try:
            sess = ClientMixin.get_new_session()
            status, result = ClientMixin.make_request(url, method="get", session=sess, logger=self.log,
                                                      TIMEOUT=self.collection_config['TIMEOUT'],
                                                      MAX_RETRY=self.collection_config['MAX_RETRY'],
                                                      BACKOFF_FACTOR=self.collection_config['BACKOFF_FACTOR'])
            if status and result is not None:
                if "actions" in result:
                    actions = result["actions"]
                    for actionName, values in actions.items():
                        if "workspace_or_org" == actionName:
                            self.WorkspaceAuditActions = values
                        elif "user" == actionName:
                            self.UserAuditActions = values
                        elif "file" == actionName:
                            self.ChannelAuditActions = values
                        elif "channel" == actionName:
                            self.FileAuditActions = values
                        elif "app" == actionName:
                            self.AppAuditActions = values
                        else:
                            if hasattr(self, "OtherAuditActions"):
                                self.OtherAuditActions.extend(values)
                            else:
                                self.OtherAuditActions = values
        except Exception as exc:
            self.log.error("Error Occurred while fetching Audit Actions Error %s", exc)

    def run(self, *args, **kwargs):
        if self.is_running():
            try:
                self.log.info('Starting Slack Sumo Collector...')
                task_params = self.build_task_params()
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
