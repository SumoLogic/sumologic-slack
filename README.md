# sumologic-slack

Solution to pull logs from Slack to Sumo Logic


## Installation

This collector can be deployed both onprem and on cloud.


### Deploying the collector on a VM
1. Get Token from Slack for your workspace/Team. 
    * [Token and Authentication details](https://slack.dev/python-slackclient/auth.html) from slack
    * Generating a [Slack API token](https://get.slack.help/hc/en-us/articles/215770388-Create-and-regenerate-API-tokens)
    
2. Add a Hosted Collector and one HTTP Logs Source

    * To create a new Sumo Logic Hosted Collector, perform the steps in [Configure a Hosted Collector](https://help.sumologic.com/03Send-Data/Hosted-Collectors/Configure-a-Hosted-Collector).
    * Add an [HTTP Logs and Metrics Source](https://help.sumologic.com/03Send-Data/Sources/02Sources-for-Hosted-Collectors/HTTP-Source).

3. Using the **sumologic-slack** collector 
    * **Method 1** - Configuring the **sumologic-slack** collector

        Below instructions assume pip is already installed if not then, see the pip [docs](https://pip.pypa.io/en/stable/installing/) on how to download and install pip.
    *sumologic-slack* is compatible with python 3.7 and python 2.7. It has been tested on Ubuntu 18.04 LTS and Debian 4.9.130.
    Login to a Linux machine and download and follow the below steps:

        * Install the collector using below command
      ``` pip install sumologic-slack```

        * Create a configuration file named slackcollector.yaml in home directory by copying the below snippet.

            ```
            Slack:
                TOKEN: <Paste the Token collected from Slack App from step 1.>
                ENABLE_INFREQUENT_CHANNELS: < Default is false.
                                              true -> Enable dividing channels into frequent and infrequent based on the last message time.
                                              false -> Send all public channels messages.>
                INFREQUENT_CHANNELS_THRESHOLD_IN_HOURS: < Default is 72.
                                                          Threshold in hours to make channels as infrequent based on last message time. 
                                                          For eg, 12 hours means if the message is not recived for 12 hours, channel will be marked as infrequent.>
                INFREQUENT_CHANNELS_MESSAGES_FETCH_TIME_IN_HOURS: < Default is 12.
                                                                    Time in hours to fetch messages for InFrequent channels.
                                                                    For eg, 12 hours means send infrequent channels messages every 12 hours.>
            Collection:
                BACKFILL_DAYS: <Enter the Number of days before the event collection will start.>
 
            SumoLogic:
                HTTP_LOGS_ENDPOINT: <Paste the URL for the HTTP Logs source from step 2.>
            ```
    * Create a cron job  for running the collector every 5 minutes by using the crontab -e and adding the below line

        `*/5 * * * *  /usr/bin/python -m sumoslack.main > /dev/null 2>&1`

   * **Method 2** - Collection via an AWS Lambda function
   
        To install Sumo Logic’s AWS Lambda script, follow the instructions below:

        * Go to https://serverlessrepo.aws.amazon.com/applications
        * Search for “sumologic-slack” and select the app as shown below:
        ![App](https://appdev-readme-resources.s3.amazonaws.com/slack/App.png)

        * In the Configure application parameters panel, shown below:
        ![Deploy](https://appdev-readme-resources.s3.amazonaws.com/slack/Deploy.png)

            ```
            Token: Paste the Token collected from Slack App from step 1.
            HttpLogsEndpoint: Paste the URL for the HTTP Logs source from step 2.
            BackfillDays: Enter the Number of days before the event collection will start
            DatabaseName: Enter the DataBase Name. 
            EnableInfrequentChannels: Default is false. 
                                      true -> Enable dividing channels into frequent and infrequent based on the last message time.
                                      false -> Send all public channels messages.
            InfrequentChannelsThresholdInHours: Default is 72.
                                                Threshold in hours to make channels as infrequent based on last message time. 
                                                For eg, 12 hours means if the message is not recived for 12 hours, channel will be marked as infrequent.
            InfrequentChannelsMessagesFetchTimeInHours: Default is 12.
                                                        Time in hours to fetch messages for InFrequent channels.
                                                        For eg, 12 hours means send infrequent channels messages every 12 hours.
            ```
        * Click Deploy.

