import time

import sys

sys.path.insert(0, '/opt')  # layer packages are in opt directory

from slackclient import SlackClient
from sumoappclient.common.utils import get_current_timestamp
from sumoappclient.sumoclient.base import BaseAPI
from sumoappclient.sumoclient.factory import OutputHandlerFactory
from sumoappclient.sumoclient.httputils import ClientMixin


class SlackAPI(BaseAPI):
    MOVING_WINDOW_DELTA = 1

    def __init__(self, kvstore, config, team_name):
        super(SlackAPI, self).__init__(kvstore, config)
        self.team_name = team_name

        # Set Slack configuration and create Slack Client
        self.api_config = self.config['Slack']
        self.token = self.config['Slack']['TOKEN']
        self.slackClient = SlackClient(self.token)

    def get_window(self, last_time_epoch):
        start_time_epoch = last_time_epoch + self.MOVING_WINDOW_DELTA
        end_time_epoch = get_current_timestamp() - self.collection_config['END_TIME_EPOCH_OFFSET_SECONDS']
        while end_time_epoch < start_time_epoch:
            # initially last_time_epoch is same as current_time_stamp so endtime becomes lesser than starttime
            end_time_epoch = get_current_timestamp()
        return start_time_epoch, end_time_epoch


class FetchCursorBasedData(SlackAPI):

    @staticmethod
    def _next_cursor_is_present(result):
        """Determine if the response contains 'next_cursor'
        and 'next_cursor' is not empty.

        Returns:
            A boolean value.
        """
        present = (
                "response_metadata" in result
                and "next_cursor" in result["response_metadata"]
                and result["response_metadata"]["next_cursor"] != ""
        )
        return present

    def fetch(self):
        next_request = True
        method, args = self.build_fetch_params()
        retry_counter = 0
        page_counter = 0
        record_counter = 0
        output_handler = OutputHandlerFactory.get_handler(self.collection_config['OUTPUT_HANDLER'],
                                                          config=self.config)
        try:
            while next_request:
                send_success = retry_request = has_next_cursor = False

                result = self.slackClient.api_call(method, self.collection_config['TIMEOUT'], **args)
                fetch_success = result["ok"]
                if fetch_success:
                    data_to_be_sent = self.transform_data(result)
                    send_success = output_handler.send(data_to_be_sent, **self.build_send_params())
                    if send_success:
                        page_counter += 1
                        record_counter += len(data_to_be_sent)
                        has_next_cursor = self._next_cursor_is_present(result)
                        if has_next_cursor:
                            args["cursor"] = result["response_metadata"]["next_cursor"]
                            self.save_state(args["cursor"], data_to_be_sent)
                        else:
                            self.save_state(None, data_to_be_sent)
                    else:
                        self.save_state(args["cursor"], [])
                else:
                    if "error" in result and result["error"].startswith("invalid_cursor"):
                        self.save_state(None, [])
                    elif "Retry-After" in result["headers"]:
                        # The `Retry-After` header will tell you how long to wait before retrying
                        delay = int(result["headers"]["Retry-After"])
                        self.log.warning("Rate limited. Retrying in %s seconds", str(delay))
                        time.sleep(delay)
                        # set the counter for retry
                        retry_counter += 1
                        # retry only for Max Retry times
                        if retry_counter <= self.collection_config["MAX_RETRY"]:
                            self.log.debug("Retrying the method %s for %s", method, args["counter"])
                            retry_request = True
                        else:
                            retry_request = False
                    else:
                        self.log.warning("Failed to fetch LogType %s, Cursor %s, Error %s", method, args["cursor"],
                                         result["error"])

                if retry_request:
                    next_request = True
                else:
                    next_request = fetch_success and send_success and has_next_cursor and self.is_time_remaining()
        finally:
            output_handler.close()
        self.log.info("Completed LogType %s, Pages: %s, Records %s", method, page_counter, record_counter)


class FetchPaginatedDataBasedOnLatestAndOldestTimeStamp(SlackAPI):

    def fetch(self):
        output_handler = OutputHandlerFactory.get_handler(self.collection_config['OUTPUT_HANDLER'],
                                                          config=self.config)
        method, args = self.build_fetch_params()
        current_state = self.get_state()
        next_request = True
        page_counter = 0
        record_counter = 0

        try:
            while next_request:
                send_success = has_more_data = False
                result = self.slackClient.api_call(method, self.collection_config['TIMEOUT'], **args)
                fetch_success = result["ok"]
                if fetch_success:
                    data_to_be_sent = self.transform_data(result)
                    if len(data_to_be_sent) > 0:
                        send_success = output_handler.send(data_to_be_sent, **self.build_send_params())
                        if send_success:
                            page_counter += 1
                            record_counter += len(data_to_be_sent)
                            last_record_fetched_timestamp = data_to_be_sent[-1]["ts"]
                            self.log.debug("Successfully sent LogType %s, oldest %s, latest %s, number of records %s",
                                           method, args["oldest"], args["latest"], len(data_to_be_sent))

                            if "has_more" in result and result["has_more"]:
                                has_more_data = True
                                args["latest"] = float(last_record_fetched_timestamp) - 0.00001
                                self.save_state(
                                    {"fetch_oldest": current_state["fetch_oldest"],
                                     "fetch_latest": current_state["fetch_latest"],
                                     "last_record_fetched_timestamp": last_record_fetched_timestamp})
                            else:
                                self.log.debug("moving time window for LogType %s, %s, oldest %s, latest %s", method,
                                               self.channel_name, args["oldest"],
                                               args["latest"])
                                self.save_state({"fetch_oldest": current_state["fetch_latest"], "fetch_latest": None,
                                                 "last_record_fetched_timestamp": None})
                        else:
                            self.log.warning("Failed to sent LogType %s, %s, oldest %s, latest %s", method,
                                             self.channel_name, args["oldest"],
                                             args["latest"])
                    else:
                        self.log.debug("No Result found for %s, Oldest %s, Latest %s", self.channel_name,
                                       args["oldest"],
                                       args["latest"])
                        self.save_state({"fetch_oldest": current_state["fetch_oldest"],
                                         "fetch_latest": None,
                                         "last_record_fetched_timestamp": None})
                else:
                    self.log.warning("Failed to fetch LogType %s, %s, oldest %s, latest %s, error %s", method,
                                     self.channel_name,
                                     args["oldest"], args["latest"], result["error"])
                next_request = fetch_success and send_success and has_more_data and self.is_time_remaining()
        except Exception as exc:
            self.log.error("Error Occurred while fetching LogType %s, %s, Error %s", method, self.channel_name,
                           exc)
        finally:
            output_handler.close()
        self.log.info("Completed LogType %s, %s, Pages: %s, Records %s", method, self.channel_name, page_counter,
                      record_counter)


class FetchPaginatedDataBasedOnPageNumber(SlackAPI):

    def fetch(self):
        method, args = self.build_fetch_params()
        output_handler = OutputHandlerFactory.get_handler(self.collection_config['OUTPUT_HANDLER'],
                                                          config=self.config)
        try:
            result = self.slackClient.api_call(method, self.collection_config['TIMEOUT'], **args)
            fetch_success = result["ok"]
            if fetch_success:
                data_to_be_sent = self.transform_data(result)
                if len(data_to_be_sent) > 0:
                    send_success = output_handler.send(data_to_be_sent, **self.build_send_params())
                    if send_success:
                        self.save_state(result["paging"])
                        self.log.debug("Sent successfully for LogType %s, Page %s, Before %s, Records %s", method,
                                       self.page, args["before"], len(data_to_be_sent))
                    else:
                        self.log.warning("Send failed for LogType %s, Page %s, Before %s", method, self.page,
                                         args["before"])
                else:
                    self.save_state(result["paging"])
                    self.log.debug("No Result fetched for LogType %s, Page %s, Before %s", method, self.page,
                                   args["before"])
            else:
                self.log.warning("Fetch failed for LogType %s, Page %s, Before %s, Error %s", method, self.page,
                                 args["before"], result["error"])
        except Exception as exc:
            self.log.error("Error Occurred while fetching LogType %s, Page %s, Before %s, Error %s", method, self.page,
                           args["before"], exc)

        self.log.info("Completed LogType %s, Page %s, Before %s", method, self.page, args["before"])


class FetchAuditData(FetchCursorBasedData):

    def fetch(self):
        output_handler = OutputHandlerFactory.get_handler(self.collection_config['OUTPUT_HANDLER'],
                                                          config=self.config)
        url, args = self.build_fetch_params()
        current_state = self.get_state()
        log_type = self.get_key()
        next_request = True
        page_counter = 0
        record_counter = 0
        sess = ClientMixin.get_new_session()

        try:
            while next_request:
                send_success = has_more_data = False
                status, result = ClientMixin.make_request(url, method="get", session=sess, logger=self.log,
                                                          TIMEOUT=self.collection_config['TIMEOUT'],
                                                          MAX_RETRY=self.collection_config['MAX_RETRY'],
                                                          BACKOFF_FACTOR=self.collection_config['BACKOFF_FACTOR'],
                                                          params=args,
                                                          headers={"Authorization": "Bearer " + self.token})
                fetch_success = status and "entries" in result
                if fetch_success:
                    data_to_be_sent = self.transform_data(result)
                    if len(data_to_be_sent) > 0:
                        send_success = output_handler.send(data_to_be_sent, **self.build_send_params())
                        if send_success:
                            page_counter += 1
                            record_counter += len(data_to_be_sent)
                            last_record_fetched_timestamp = data_to_be_sent[-1]["date_create"]
                            self.log.debug("Successfully sent LogType %s, oldest %s, latest %s, number of records %s",
                                           log_type, args["latest"], args["oldest"], len(data_to_be_sent))

                            args["latest"] = float(last_record_fetched_timestamp) - 0.00001
                            if self._next_cursor_is_present(result):
                                has_more_data = True
                                args["latest"] = float(last_record_fetched_timestamp) - 0.00001
                                self.save_state(
                                    {"fetch_oldest": current_state["fetch_oldest"],
                                     "fetch_latest": current_state["fetch_latest"],
                                     "last_record_fetched_timestamp": last_record_fetched_timestamp})
                            else:
                                self.log.debug("moving time window for LogType %s, oldest %s, latest %s",
                                               self.get_key(),
                                               args["oldest"], args["latest"])
                                self.save_state({"fetch_oldest": current_state["fetch_latest"], "fetch_latest": None,
                                                 "last_record_fetched_timestamp": None})
                        else:
                            self.log.warning("Failed to sent LogType %s, oldest %s, latest %s", log_type,
                                             args["oldest"], args["latest"])
                    else:
                        self.log.debug("No Result found for %s, Oldest %s, Latest %s", log_type, args["oldest"],
                                       args["latest"])
                        self.save_state({"fetch_oldest": current_state["fetch_oldest"],
                                         "fetch_latest": None, "last_record_fetched_timestamp": None})
                else:
                    self.log.warning("Failed to fetch LogType %s, oldest %s, latest %s, error %s", log_type,
                                     args["oldest"], args["latest"], result["error"])
                next_request = fetch_success and send_success and has_more_data and self.is_time_remaining()
        except Exception as exc:
            self.log.error("Error Occurred while fetching LogType %s, Error %s", log_type,
                           exc)
        finally:
            output_handler.close()
            sess.close()
        self.log.info("Completed LogType %s, Pages: %s, Records %s", log_type, page_counter,
                      record_counter)


class UsersDataAPI(FetchCursorBasedData):

    def __init__(self, kvstore, config, team_name, data_refresh_time):
        super(UsersDataAPI, self).__init__(kvstore, config, team_name)
        self.data_refresh_time = data_refresh_time

    def get_key(self):
        return "Users"

    def save_state(self, cursor, users):
        self.kvstore.set(self.get_key(), cursor)
        if len(users) > 0:
            for user_data in users:
                self.kvstore.set(user_data["id"],
                                 {"updated": user_data["updated"], "lastSent": get_current_timestamp(),
                                  "user_name": user_data["name"]})

    def get_state(self):
        key = self.get_key()
        if not self.kvstore.has_key(key):
            return None
        cursor = self.kvstore.get(key)
        return cursor

    def build_fetch_params(self):
        return "users.list", {"include_locale": True, "limit": 200, "cursor": self.get_state()}

    def build_send_params(self):
        return {
            "endpoint_key": "HTTP_LOGS_ENDPOINT"
        }

    def transform_data(self, content):
        transformed_users = []
        if content is not None and "members" in content:
            for user_data in content["members"]:
                transformed_user_data = self._transform_user_data(user_data)
                if transformed_user_data is not None:
                    transformed_users.append(transformed_user_data)
        return transformed_users

    def _transform_user_data(self, user_data):
        user_id = user_data["id"]

        email = "-"
        if "profile" in user_data and "email" in user_data["profile"]:
            email = user_data["profile"]["email"]

        # check if the data is present in key value store and send only if there is any change in user data.
        last_updated = None
        last_sent = None
        if self.kvstore.has_key(user_id):
            user = self.kvstore.get(user_id)
            last_updated = user["updated"]
            last_sent = user["lastSent"]

        # Send user data every 24 hours and meanwhile if updated send it
        if last_updated == user_data["updated"] and get_current_timestamp() - last_sent < self.data_refresh_time:
            self.log.debug("user already present")
        else:
            transformed_user_data = {"id": user_data.get("id"), "name": user_data.get("name"),
                                     "deleted": user_data.get("deleted", False),
                                     "real_name": user_data.get("real_name", "-"), "tz": user_data.get("tz", "-"),
                                     "tz_label": user_data.get("tz_label", "-"),
                                     "is_admin": user_data.get("is_admin", False),
                                     "is_owner": user_data.get("is_owner", False),
                                     "is_primary_owner": user_data.get("is_primary_owner", False),
                                     "is_restricted": user_data.get("is_restricted", False),
                                     "is_ultra_restricted": user_data.get("is_ultra_restricted", False),
                                     "is_bot": user_data.get("is_bot", False),
                                     "is_app_user": user_data.get("is_app_user", False),
                                     "updated": user_data.get("updated"), "has_2fa": user_data.get("has_2fa", False),
                                     "teamName": self.team_name, "email": email,
                                     "billable": self._billing_info(user_id), "logType": "UserLog"}
            return transformed_user_data
        return None

    def _billing_info(self, user_id):
        data = self.slackClient.api_call("team.billableInfo", user=user_id)
        if data is not None and "billable_info" in data and user_id in data["billable_info"]:
            billing = data["billable_info"][user_id]
            return billing["billing_active"]
        return False


class ChannelsDataAPI(FetchCursorBasedData):
    frequent = "frequent_"
    in_frequent = "in_frequent_"

    def __init__(self, kvstore, config, team_name, channel_page_number, infrequent_channel_threshold
                 , frequent_channels_to_be_sent, infrequent_channels_to_be_sent, enable_infrequent_channels):
        super(ChannelsDataAPI, self).__init__(kvstore, config, team_name)
        self.channel_page_number = channel_page_number
        # the max difference between current timestamp and last oldest fetched timestamp to mark a channel as infrequent
        self.infrequent_channel_threshold = infrequent_channel_threshold
        self.infrequent_channels_to_be_sent = infrequent_channels_to_be_sent
        self.frequent_channels_to_be_sent = frequent_channels_to_be_sent
        self.enable_infrequent_channels = enable_infrequent_channels

    def get_key(self):
        return "Channels_"

    # Saving state as per the channels calls for limit of 50. Keys will be like Channels_1, Channels_2, Channels_3 ....
    # Done to solve issue "Item size has exceeded the maximum allowed size"
    def save_state(self, cursor, data):
        # Get frequent channels current page
        frequent_channel_page_number = self.kvstore.get("frequent_channel_page_number")
        frequent_channel_page_number = 1 if frequent_channel_page_number is None else frequent_channel_page_number

        frequent_channels = self.kvstore.get(self.get_key() + self.frequent + str(frequent_channel_page_number))
        frequent_channels = [] if frequent_channels is None or frequent_channels["ids"] is None \
            else frequent_channels["ids"]

        # Get in-frequent channels current page
        in_frequent_channel_page_number = self.kvstore.get("in_frequent_channel_page_number")
        in_frequent_channel_page_number = 1 if in_frequent_channel_page_number is None \
            else in_frequent_channel_page_number

        infrequent_channels = self.kvstore.get(self.get_key() + self.in_frequent + str(in_frequent_channel_page_number))
        infrequent_channels = [] if infrequent_channels is None or infrequent_channels["ids"] is None \
            else infrequent_channels["ids"]

        # Update the frequent and infrequent list as per threshold provided by user
        if data is not None:
            for channel in data:
                channel_id = channel["channel_id"]
                channel_name = channel["channel_name"]
                messages_details = self.kvstore.get(channel_id)
                if self.enable_infrequent_channels \
                        and messages_details is not None \
                        and "fetch_oldest" in messages_details \
                        and get_current_timestamp() - messages_details.get("fetch_oldest") > \
                        self.infrequent_channel_threshold:
                    infrequent_channels.append(channel_id + "#" + channel_name)
                else:
                    frequent_channels.append(channel_id + "#" + channel_name)

        # segregate list into chunks of User provided chunks and save them in database.
        self.put_channels_data(frequent_channels, frequent_channel_page_number, self.frequent
                               , cursor, self.frequent_channels_to_be_sent)
        self.put_channels_data(infrequent_channels, in_frequent_channel_page_number, self.in_frequent
                               , cursor, self.infrequent_channels_to_be_sent)

    def get_state(self):
        key = self.get_key() + str(self.channel_page_number)
        if not self.kvstore.has_key(key):
            return None
        obj = self.kvstore.get(key)
        return obj

    def build_fetch_params(self):
        cursor = None
        self.channel_page_number = self.frequent + str(self.kvstore.get("frequent_channel_page_number"))
        obj = self.get_state()
        if obj is not None and "cursor" in obj:
            cursor = obj["cursor"]
        return "conversations.list", {"types": "public_channel", "limit": 200, "cursor": cursor,
                                      "exclude_archived": True}

    def build_send_params(self):
        return {
            "endpoint_key": "HTTP_LOGS_ENDPOINT"
        }

    def transform_data(self, content):
        channel_details = []
        if content is not None and "channels" in content:
            for channel in content["channels"]:
                if channel is not None:
                    channel_details.append(
                        {"channel_id": channel["id"], "channel_name": channel["name"],
                         "members": channel["num_members"],
                         "logType": "ChannelDetail", "teamName": self.team_name})
        return channel_details

    def put_channels_data(self, channels, number, key, cursor, channels_to_be_sent):
        ids = self.batchsize_chunking(channels, channels_to_be_sent)
        for channels in ids:
            obj = {"ids": channels, "last_fetched": get_current_timestamp(), "cursor": cursor}
            self.kvstore.set(self.get_key() + key + str(number), obj)
            self.kvstore.set(key + "channel_page_number", number)
            number = number + 1

    def batchsize_chunking(cls, iterable, size=1):
        l = len(iterable)
        for idx in range(0, l, size):
            data = iterable[idx:min(idx + size, l)]
            yield data


class ChannelsMessagesAPI(FetchPaginatedDataBasedOnLatestAndOldestTimeStamp):

    def __init__(self, kvstore, config, channel_id, channel_name, team_name):
        super(ChannelsMessagesAPI, self).__init__(kvstore, config, team_name)
        self.channel_id = channel_id
        self.channel_name = channel_name

    def get_key(self):
        return self.channel_id

    def save_state(self, state):
        self.kvstore.set(self.get_key(), state)

    def get_state(self):
        key = self.get_key()
        if not self.kvstore.has_key(key):
            self.save_state({"fetch_oldest": self.DEFAULT_START_TIME_EPOCH, "fetch_latest": None,
                             "last_record_fetched_timestamp": None})
        obj = self.kvstore.get(key)
        return obj

    def build_fetch_params(self):
        state = self.get_state()
        latest = None

        if "fetch_latest" not in state or ("fetch_latest" in state and state["fetch_latest"] is None):
            oldest, latest = self.get_window(state["fetch_oldest"])
            self.save_state({"fetch_oldest": oldest, "fetch_latest": latest,
                             "last_record_fetched_timestamp": None})
        else:
            oldest = state["fetch_oldest"]
            # to be sure every data has been fetched in case of previous failure
            if "fetch_latest" in state and state["fetch_latest"] is not None:
                latest = state["fetch_latest"]
            if "last_record_fetched_timestamp" in state and state["last_record_fetched_timestamp"] is not None:
                latest = state["last_record_fetched_timestamp"]

        return "conversations.history", {"channel": self.get_key(), "inclusive": True, "latest": latest,
                                         "oldest": oldest, "limit": 500}

    def build_send_params(self):
        return {
            "endpoint_key": "HTTP_LOGS_ENDPOINT"
        }

    def transform_data(self, content):
        if "messages" in content and len(content["messages"]) > 0:
            messages = content["messages"]
            for data in messages:
                if "files" in data:
                    files = []
                    for file_data in data["files"]:
                        modified_file_data = {"name": file_data["name"], "fileType": file_data["filetype"],
                                              "fileSize": file_data.get("size", 0),
                                              "urlPrivate": file_data.get("url_private", ""),
                                              "urlPrivateDownload": file_data.get("url_private_download", ""),
                                              "permalink": file_data.get("permalink", "")}
                        files.append(modified_file_data)
                    data["files"] = files

                if "attachments" in data:
                    attachments = []
                    for attachment_data in data["attachments"]:
                        modified_attachment_data = {"id": attachment_data["id"],
                                                    "text": attachment_data.get("text", ""),
                                                    "author_name": attachment_data.get("author_name", ""),
                                                    "author_link": attachment_data.get("author_link", ""),
                                                    "pretext": attachment_data.get("pretext", ""),
                                                    "fallback": attachment_data.get("fallback", "")}
                        attachments.append(modified_attachment_data)
                    data["attachments"] = attachments

                if "user" in data and self.kvstore.has_key(data["user"]):
                    data["userName"] = self.kvstore.get(data["user"])["user_name"]

                data["channelId"] = self.channel_id
                data["channelName"] = self.channel_name
                data["teamName"] = self.team_name
                data["logType"] = "ConversationLog"

                if "is_starred" in data:
                    data.pop("is_starred")
                if "pinned_to" in data:
                    data.pop("pinned_to")
                if "reactions" in data:
                    data.pop("reactions")
            return messages
        return []


class AccessLogsAPI(FetchPaginatedDataBasedOnPageNumber):

    def __init__(self, kvstore, config, page, team_name):
        super(AccessLogsAPI, self).__init__(kvstore, config, team_name)
        self.page = page

    def get_key(self):
        return "AccessLogs"

    def save_state(self, state):
        if "pages" in state:
            if self.page == 1:
                self.kvstore.set("Access_logs_max_page", state["pages"] + 1)
        else:
            self.kvstore.set(self.get_key(), state)

    def get_state(self):
        key = self.get_key()
        if not self.kvstore.has_key(key):
            oldest, latest = self.get_window(get_current_timestamp())
            self.save_state({"fetch_before": latest})
        obj = self.kvstore.get(key)
        return obj

    def build_fetch_params(self):
        state = self.get_state()
        fetch_before = None

        if "fetch_before" in state:
            fetch_before = state["fetch_before"]

        return "team.accessLogs", {"count": 1000, "before": fetch_before, "page": self.page}

    def build_send_params(self):
        return {
            "endpoint_key": "HTTP_LOGS_ENDPOINT"
        }

    def transform_data(self, content):
        data = []
        if content is not None and "logins" in content:
            logs = content["logins"]
            for log in logs:
                log["teamName"] = self.team_name
                log["logType"] = "AccessLog"
                if "date_last" in log and log["date_last"] > self.kvstore.get("Access_logs_Previous_before_time", 0):
                    data.append(log)
        return data


class AuditLogsAPI(FetchAuditData):
    def __init__(self, kvstore, config, url, team_name, workspaceauditactions, userauditactions, channelauditactions,
                 fileauditactions, appauditactions, otherauditactions):
        super(AuditLogsAPI, self).__init__(kvstore, config, team_name)
        self.url = url + "logs"
        self.WorkspaceAuditActions = workspaceauditactions
        self.UserAuditActions = userauditactions
        self.ChannelAuditActions = channelauditactions
        self.FileAuditActions = fileauditactions
        self.AppAuditActions = appauditactions
        self.OtherAuditActions = otherauditactions
        if "ExcludeAuditLog" in self.api_config and self.api_config["ExcludeAuditLog"] is not None:
            self.excludeList = self.api_config["ExcludeAuditLog"]

    def get_key(self):
        return "AuditLogs"

    def save_state(self, state):
        self.kvstore.set(self.get_key(), state)

    def get_state(self):
        key = self.get_key()
        if not self.kvstore.has_key(key):
            self.save_state({"fetch_oldest": self.DEFAULT_START_TIME_EPOCH, "fetch_latest": None,
                             "last_record_fetched_timestamp": None})
        obj = self.kvstore.get(key)
        return obj

    def build_fetch_params(self):
        state = self.get_state()
        latest = None

        if "fetch_latest" not in state or ("fetch_latest" in state and state["fetch_latest"] is None):
            oldest, latest = self.get_window(state["fetch_oldest"])
            self.save_state({"fetch_oldest": oldest, "fetch_latest": latest,
                             "last_record_fetched_timestamp": None})
        else:
            oldest = state["fetch_oldest"]
            # to be sure every data has been fetched in case of previous failure
            if "fetch_latest" in state and state["fetch_latest"] is not None:
                latest = state["fetch_latest"]
            if "last_record_fetched_timestamp" in state and state["last_record_fetched_timestamp"] is not None:
                latest = state["last_record_fetched_timestamp"]

        return self.url, {"latest": latest, "oldest": oldest, "inclusive": True, "limit": 9999}

    def build_send_params(self):
        return {
            "endpoint_key": "HTTP_LOGS_ENDPOINT"
        }

    def transform_data(self, content):
        data_to_be_sent = []
        if content is not None and "entries" in content:
            entries = content["entries"]
            for entry in entries:
                action = entry["action"]
                if hasattr(self, "WorkspaceAuditActions") and action in self.WorkspaceAuditActions:
                    entry["logType"] = "WorkspaceAuditLog"
                elif hasattr(self, "UserAuditActions") and action in self.UserAuditActions:
                    entry["logType"] = "UserAuditLog"
                elif hasattr(self, "ChannelAuditActions") and action in self.ChannelAuditActions:
                    entry["logType"] = "ChannelAuditLog"
                elif hasattr(self, "FileAuditActions") and action in self.FileAuditActions:
                    entry["logType"] = "FileAuditLog"
                elif hasattr(self, "AppAuditActions") and action in self.AppAuditActions:
                    entry["logType"] = "AppAuditLog"
                elif hasattr(self, "OtherAuditActions") and action in self.OtherAuditActions:
                    entry["logType"] = "OtherAuditLogs"

                # flat the entity level hierarchy
                if "entity" in entry and "type" in entry["entity"]:
                    entity = entry["entity"]
                    entity_type = entity["type"]
                    if entity_type in entity:
                        data = entity[entity_type]
                        entry["entity"] = data

                if hasattr(self, "excludeList") and action in self.excludeList:
                    self.log.debug("Audit Log Entry Skipped for Action - " + action)
                else:
                    data_to_be_sent.append(entry)
        return data_to_be_sent
