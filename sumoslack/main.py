import os
import sys
import traceback
from random import shuffle

sys.path.insert(0, '/opt')  # layer packages are in opt directory

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
    MAX_PAGE = 101

    def __init__(self):
        self.project_dir = get_current_dir()
        super(SumoSlackCollector, self).__init__(self.project_dir)

    def _set_basic_data(self):
        # Set Slack configuration and create Slack Client
        self.api_config = self.config['Slack']
        self.token = self.config['Slack']['TOKEN']
        self.slackClient = SlackClient(self.token)

        # Populate Team Name in Key Value Pair
        self.team_name = self._set_team_name()

        # Set Data refresh time for user logs
        self.user_logs_data_refresh_time = self.config['Slack']['USER_LOGS_REFRESH_TIME_IN_HOURS'] * 60 * 60

        self.frequent_channels_to_be_sent = self.config['Slack']['FREQUENT_CHANNELS_CHUNK_SIZE']

        # Infrequent channels Details
        self.infrequent_channel_messages_fetch_time = self.config['Slack'][
                                                          'INFREQUENT_CHANNELS_MESSAGES_FETCH_TIME_IN_HOURS'] * 60 * 60
        self.infrequent_channel_threshold = self.config['Slack']['INFREQUENT_CHANNELS_THRESHOLD_IN_HOURS'] * 60 * 60
        self.infrequent_channels_to_be_sent = self.config['Slack']['INFREQUENT_CHANNELS_CHUNK_SIZE']
        self.enable_infrequent_channels = self.config['Slack']['ENABLE_INFREQUENT_CHANNELS']
        if type(self.enable_infrequent_channels) != bool:
            self.enable_infrequent_channels = True if self.enable_infrequent_channels.lower() == "true" else False

        self.PAGE_COUNTER = self.config['Slack']['ACCESS_LOGS_PAGE_COUNTER']

    def _set_team_name(self):
        data = self.slackClient.api_call("team.info", self.collection_config['TIMEOUT'])
        if data is not None and data["ok"] and "team" in data:
            self.kvstore.set(data["team"]["id"], data["team"]["name"])
            return data["team"]["name"]
        else:
            self.log.error("Team name call failed with error as %s", data["error"])
            sys.exit()

    def build_task_params(self):
        self.log.info("Building task Parameters............")
        tasks = []
        shuffle_tasks = []
        self._set_basic_data()
        if 'LOG_TYPES' in self.api_config:
            # ************** USER LOGS PROCESS **************
            if "USER_LOGS" in self.api_config['LOG_TYPES']:
                tasks.append(UsersDataAPI(self.kvstore, self.config, self.team_name, self.user_logs_data_refresh_time))

            # ************** CHANNEL LOGS PROCESS **************

            # Get frequent and infrequent channel list. Call infrequent channels based on last call time.
            call_in_frequent_channels = False
            # check if infrequent channels need to be called
            if self.enable_infrequent_channels and \
                    get_current_timestamp() - self.kvstore.get("in_frequent_channel_last_call_time", 0) \
                    > self.infrequent_channel_messages_fetch_time:
                self.log.info("Infrequent channels will be sent")
                call_in_frequent_channels = True

            if call_in_frequent_channels:
                channels = self._get_channel_ids("in_frequent_")
                if self.kvstore.get("in_frequent_channel_page_current_index") \
                        == self.kvstore.get("in_frequent_channel_page_number"):
                    self.kvstore.set("in_frequent_channel_last_call_time", get_current_timestamp())
            else:
                channels = self._get_channel_ids("frequent_")

            if "CHANNELS_MESSAGES_LOGS" in self.api_config['LOG_TYPES']:
                # Append all channels to shuffle tasks
                if channels is not None and "ids" in channels:
                    channels_ids = channels["ids"]
                    for channels_id in channels_ids:
                        channel = channels_id.split("#")
                        shuffle_tasks.append(
                            ChannelsMessagesAPI(self.kvstore, self.config, channel[0], channel[1], self.team_name))

            # ************** ACCESS LOGS PROCESS **************
            if "ACCESS_LOGS" in self.api_config['LOG_TYPES']:
                page = self.kvstore.get("Access_logs_page_index", 1)
                next_page = page + self.PAGE_COUNTER

                max_page = min(self.kvstore.get("Access_logs_max_page", next_page), self.MAX_PAGE)

                if page >= max_page:
                    self.kvstore.set("Access_logs_page_index", 1)
                    self.kvstore.set("Access_logs_Previous_before_time",
                                     self.kvstore.get("AccessLogs").get("fetch_before"))
                    self.kvstore.delete("AccessLogs")
                    self.kvstore.delete("Access_logs_max_page")
                else:
                    for page_number in range(page, next_page):
                        tasks.append(AccessLogsAPI(self.kvstore, self.config, page_number, self.team_name))
                    self.kvstore.set("Access_logs_page_index", next_page)

            # ************** AUDIT LOGS PROCESS **************
            if "AUDIT_LOGS" in self.api_config['LOG_TYPES'] and "AUDIT_LOG_URL" in self.api_config:
                self._get_audit_actions(self.api_config["AUDIT_LOG_URL"])
                shuffle_tasks.append(
                    AuditLogsAPI(self.kvstore, self.config, self.api_config["AUDIT_LOG_URL"], self.team_name,
                                 self.WorkspaceAuditActions, self.UserAuditActions, self.ChannelAuditActions,
                                 self.FileAuditActions, self.AppAuditActions, self.OtherAuditActions))

        shuffle(shuffle_tasks)
        tasks.extend(shuffle_tasks)
        self.log.info("Building task Parameters Done.")
        return tasks

    def _get_channel_ids(self, key):
        next_counter = self.kvstore.get(key + "channel_page_current_index", 0) + 1
        channels_data = ChannelsDataAPI(self.kvstore, self.config, self.team_name,
                                        key + str(next_counter), self.infrequent_channel_threshold,
                                        self.frequent_channels_to_be_sent, self.infrequent_channels_to_be_sent,
                                        self.enable_infrequent_channels)

        if next_counter > self.kvstore.get(key + "channel_page_number", 0):
            if "CHANNELS_LOGS" in self.api_config['LOG_TYPES']:
                # Before fetching delete all keys for channels.
                self._delete_channel_keys("frequent_", channels_data)
                self._delete_channel_keys("in_frequent_", channels_data)
                channels_data.fetch()
            return None
        else:
            self.log.info("Channels Data will not be fetched as all channels message data is not sent.")

        obj = channels_data.get_state()
        self.kvstore.set(key + "channel_page_current_index", next_counter)
        return obj

    def _delete_channel_keys(self, key, channels_data):
        number = self.kvstore.get(key + "channel_page_number")
        if number:
            for i in range(1, number + 1):
                self.kvstore.delete(channels_data.get_key() + key + str(i))
        self.kvstore.delete(key + "channel_page_number")
        self.kvstore.delete(key + "channel_page_current_index")

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
        finally:
            sess.close()


def main(*args, **kwargs):
    try:
        ns = SumoSlackCollector()
        ns.run()
        # ns.test()
    except BaseException as e:
        traceback.print_exc()


if __name__ == '__main__':
    main()
