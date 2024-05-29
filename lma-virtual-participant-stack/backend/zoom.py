import asyncio
import boto3
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import json
import os
from playwright.async_api import async_playwright
import re
import sounddevice as sd
import string
from presigned_url import AWSTranscribePresignedURL
from eventstream import create_audio_event, decode_event
import websockets
import string
import random
import logging
import sys
import datetime
import aiofiles
import math
import struct
from botocore.session import Session
from botocore.exceptions import ClientError


session = Session()

# Configure logging - change to Logging.DEBUG for details
logging.basicConfig(stream=sys.stderr, level=logging.INFO) 

logging.info('Starting up...')

# Create a Boto3 session using the instance profile credentials
session = Session()
credentials = session.get_credentials()

# Configure access - either from environment variables or define them here.
# access_key = os.getenv("AWS_ACCESS_KEY_ID", "")
# secret_key = os.getenv("AWS_SECRET_ACCESS_KEY","")
# session_token = os.getenv("AWS_SESSION_TOKEN","")
access_key = credentials.access_key
secret_key = credentials.secret_key
session_token = credentials.token
region = os.getenv("AWS_DEFAULT_REGION","us-east-1")
kinesis_stream_name = os.getenv("KINESIS_STREAM_NAME")
transcribe_url_generator = AWSTranscribePresignedURL(access_key, secret_key, session_token, region)
recordings_bucket_name = os.getenv('RECORDINGS_BUCKET_NAME')
recording_file_prefix = os.getenv('RECORDINGS_KEY_PREFIX')


# Create AWS clients
kinesis = boto3.client('kinesis', region_name=region)
s3 = boto3.client('s3', region_name=region)

# Sound settings
language_code = "en-US"
media_encoding = "pcm"
sample_rate = 16000
number_of_channels = 1
channel_identification = False
bytes_per_sample = 2 # 16 bit audio
chunk_size = sample_rate * 2 * number_of_channels / 10 # roughly 100ms of audio data
file_name = 'recording.raw'
recording_file = 'recording.wav'

# Scribe specific
scribe_name = "Scribe"
email_address = os.environ['EMAIL']
meeting_id = os.environ['MEETING_ID']
lma_meeting_id = meeting_id + '-' + datetime.datetime.now().strftime('%Y-%m-%d-%H:%M:%S.%f')[:-3]
meeting_password = os.environ['MEETING_PASSWORD']
scribe_identity = f"{scribe_name} ({email_address})"

attendees = []
messages = []
attachments = {}
captions = []
speakers = []

current_speaker = ""
meeting_end = False

start = False

def get_aws_date_now():
    now = datetime.datetime.now()
    aws_datetime = now.strftime('%Y-%m-%dT%H:%M:%S.%fZ')
    return aws_datetime


def baseline_text(text: str):
    return text.lower().translate(str.maketrans('', '', string.punctuation))

async def write_wav_header(file, sample_rate, num_channels, bit_depth, num_samples):
    await file.seek(0)
    
    # RIFF header
    await file.write(b'RIFF')
    await file.write(struct.pack('<I', 36 + num_samples * num_channels * bit_depth // 8))
    await file.write(b'WAVE')
    
    # fmt chunk
    await file.write(b'fmt ')
    await file.write(struct.pack('<I', 16))
    await file.write(struct.pack('<H', 1))  # audio format (1 = PCM)
    await file.write(struct.pack('<H', num_channels))
    await file.write(struct.pack('<I', sample_rate))
    await file.write(struct.pack('<I', sample_rate * num_channels * bit_depth // 8))  # byte rate
    await file.write(struct.pack('<H', num_channels * bit_depth // 8))  # block align
    await file.write(struct.pack('<H', bit_depth))
    
    # data chunk
    await file.write(b'data')
    await file.write(struct.pack('<I', num_samples * num_channels * bit_depth // 8))

# LMA funcs

async def write_recording_s3():
    try:
        logging.info(f'Uploading recording to S3, bucket {recordings_bucket_name}...')
        num_samples = math.ceil(os.path.getsize(file_name) / 2)

        async with aiofiles.open(file_name, 'rb') as input_f, aiofiles.open(recording_file, 'wb') as output_f:
            await write_wav_header(output_f, sample_rate, number_of_channels, 16, num_samples)
            await output_f.write(await input_f.read())

        unique_filename = f'{lma_meeting_id}.wav'
        # write to s3
        s3.upload_file(recording_file, recordings_bucket_name, f'{recording_file_prefix}{unique_filename}')
        logging.info("Recording uploaded to S3")

        recording_url = f'https://{recordings_bucket_name}.s3.{region}.amazonaws.com/{recording_file_prefix}{unique_filename}'

        payload = {
            'EventType': 'ADD_S3_RECORDING_URL',
            'CallId': lma_meeting_id,
            'RecordingUrl': recording_url
        }
        logging.info(f"Sending add recording url event to Kinesis. Event: {payload}")
        # Write the messages to the Kinesis Data Stream
        response = kinesis.put_record(
            StreamName=kinesis_stream_name,
            PartitionKey=lma_meeting_id,
            Data=json.dumps(payload).encode('utf-8')
        )
        logging.info(f"Sent add recording url event to Kinesis. Response: {response}")
    except Exception as e:
        logging.exception(e)
        logging.error(f"Error sending add recording url event to Kinesis: {e}")


def send_add_transcript_segment(result):
    logging.info("Sending add transcript segment event to Kinesis")
    try:
        transcript = result['Alternatives'][0]['Transcript']
        add_transcript_segment = {
            'EventType': 'ADD_TRANSCRIPT_SEGMENT',
            'CallId': lma_meeting_id,
            'Channel': 'CALLER',
            'SegmentId': f'CALLER-${result['StartTime']}',
            'StartTime': result['StartTime'] if result['StartTime'] is not None else 0,
            'EndTime': result['StartTime'] if result['StartTime'] is not None else 0,
            'Transcript': transcript,
            'IsPartial': result['IsPartial'],
            'CreatedAt': get_aws_date_now(),
            'UpdatedAt': get_aws_date_now(),
            'Sentiment': None,
            'TranscriptEvent': None,
            'UtteranceEvent': None,
            'Speaker': current_speaker
        }
        # Write the messages to the Kinesis Data Stream
        response = kinesis.put_record(
            StreamName=kinesis_stream_name,
            PartitionKey=lma_meeting_id,
            Data=json.dumps(add_transcript_segment).encode('utf-8')
        )
        logging.info(f"Sent add transcript segment event to Kinesis. Response: {response}")
    except Exception as e:
        logging.error(f"Error sending add transcript segment event to Kinesis: {e}")

def send_start_meeting():
    try:
        start_call_event = {
            'EventType': 'START',
            'CallId': lma_meeting_id,
            'CustomerPhoneNumber': 'Customer Phone',
            'SystemPhoneNumber': 'System Phone',
            'AgentId': 'test-agent',
            'CreatedAt': get_aws_date_now()
        }
        logging.info(f"Sending start meeting event to Kinesis. Event: {start_call_event}")

        # Write the messages to the Kinesis Data Stream
        response = kinesis.put_record(
            StreamName=kinesis_stream_name,
            PartitionKey=lma_meeting_id,
            Data=json.dumps(start_call_event).encode('utf-8')
        )
        logging.info(f"Sent start meeting event to Kinesis. Response: {response}")
    except Exception as e:
        logging.error(f"Error sending start meeting event to Kinesis: {e}")


def send_end_meeting():
    try:
        start_call_event = {
            'EventType': 'END',
            'CallId': lma_meeting_id,
            'CustomerPhoneNumber': 'Customer Phone',
            'SystemPhoneNumber': 'System Phone',
            'CreatedAt': get_aws_date_now()
        }
        logging.info(f"Sending end meeting event to Kinesis. Event: {start_call_event}")

        # Write the messages to the Kinesis Data Stream
        response = kinesis.put_record(
            StreamName=kinesis_stream_name,
            PartitionKey=lma_meeting_id,
            Data=json.dumps(start_call_event).encode('utf-8')
        )
        logging.info(f"Sent end meeting event to Kinesis. Response: {response}")
    except Exception as e:
        logging.error(f"Error sending start meeting event to Kinesis: {e}")
       
# Async loop that sends file to websocket / Transcribe
async def send(websocket):
    logging.info('Starting reading from audio for transcription...')
    loop = asyncio.get_event_loop()
    input_queue = asyncio.Queue()

    def callback(indata, frame_count, time_info, status):
        loop.call_soon_threadsafe(input_queue.put_nowait, (bytes(indata), status))

    try:
        async with aiofiles.open(file_name, 'ab') as file:

            # Create the audio stream
            with sd.RawInputStream(
                channels=number_of_channels,
                samplerate=sample_rate,
                callback=callback,
                blocksize=1024 * 2, 
                dtype='int16'
                # device="pulse"
            ):
                while not meeting_end:
                    indata, status = await input_queue.get()
                    await file.write(indata)
                    if len(indata) > 0:
                        audioEvent = create_audio_event(indata) 
                        await websocket.send(audioEvent)
                    await asyncio.sleep(0)  # yield control to the event loop, also delay reading audio file
        await write_recording_s3()
    except websockets.exceptions.ConnectionClosedError:
        logging.info(f"Connection closed error")
    except Exception as error:
        logging.error(f"An exception has occurred: {error}")

# Async loop that listens for responses from Transcribe
async def receive(websocket):
    logging.info('Opening Transcribe websocket...')
    try:
        while not meeting_end:
            response = await websocket.recv()
            header, payload = decode_event(response)
            # Process the Transcribe response here.
            if header[':message-type'] == 'event':
                # this is a normal event, either TranscribeEvent or UtteranceEvent or CategoryEvent
                if len(payload['Transcript']['Results']) > 0:
                    line = payload['Transcript']['Results'][0]['Alternatives'][0]['Transcript']
                    logging.info(payload)
                    send_add_transcript_segment(payload['Transcript']['Results'][0])
                    if captions:
                        if baseline_text(captions[-1]) in baseline_text(line):
                            captions[-1] = line
                            continue
                    captions.append(line)
                    speakers.append(current_speaker)
            elif header[":message-type"] == 'exception':
                logging.info(payload['Message'])
            await asyncio.sleep(0) # Yield to main loop
    except websockets.exceptions.ConnectionClosedError as error:
        logging.error(f"Connection closed error: {error}")
    except Exception as error:
        logging.error(f"An exception has occurred: {error}")

async def transcribe():
    # generate random websocket key and headers
    websocket_key = ''.join(random.choices(string.ascii_uppercase + string.ascii_lowercase + string.digits, k=20))
    extra_headers = {
        "Origin": "https://localhost", # If on the web, replace with streaming url
        "Sec-Websocket-Key": websocket_key,
        "Sec-Websocket-Version":"13",
        "Connection":"keep-alive"
    }
    # generate signed url to connect to
    request_url = transcribe_url_generator.get_request_url(sample_rate, 
                                                           language_code, 
                                                           media_encoding, 
                                                           number_of_channels=number_of_channels,
                                                           enable_channel_identification=channel_identification)
    async with websockets.connect(request_url, 
                                  extra_headers=extra_headers, 
                                  ping_timeout=None,
                                  ) as websocket:  # Connect to the WebSocket
        await asyncio.gather(receive(websocket), send(websocket))

def deliver():

    email_source = f"{scribe_name} <{'+scribe@'.join(email_address.split('@'))}>"
    email_destinations = [email_address]

    msg = MIMEMultipart('mixed')
    msg['From'] = email_source
    msg['To'] = ', '.join(email_destinations)

    if not start:
        msg['Subject'] = os.environ['MEETING_NAME']
        body_html = body_text = "No meeting details were saved."
    else:
        attendance = '\n'.join(attendees)
        chat = '\n'.join(messages)
        transcriptions = [f"{speaker}: {caption}" for speaker, caption in zip(speakers, captions)]
        transcript = '\n\n'.join(transcriptions)

        prompt = (
            "Please create a title, summary, and list of action items from the following transcript:"
            f"\n<transcript>{transcript}</transcript>"
            "\nPlease output the title in <title></title> tags, the summary in <summary></summary> tags,"
            " and the action items in <action items></action items> tags."
        )
        body = json.dumps({
            "max_tokens": 4096,
            "messages": [{"role": "user", "content": prompt}],
            "anthropic_version": "bedrock-2023-05-31"
        })
        try: 
            response = boto3.client("bedrock-runtime").invoke_model(
                body=body, modelId="anthropic.claude-3-sonnet-20240229-v1:0"
            )
            bedrock_completion = json.loads(response.get("body").read())["content"][0]["text"]
        except Exception as e:
            logging.info(f"Error while invoking model: {e}")
            bedrock_completion = ""

        title = re.findall(r'<title>(.*?)</title>|$', bedrock_completion, re.DOTALL)[0].strip()
        summary = re.findall(r'<summary>(.*?)</summary>|$', bedrock_completion, re.DOTALL)[0].strip()
        action_items = re.findall(
            r'<action items>(.*?)</action items>|$', bedrock_completion, re.DOTALL
        )[0].strip()   

        msg['Subject'] = f"{os.environ['MEETING_NAME']} | {title}"

        body_text = "Attendees:\n" + attendance + "\nSummary:\n" + summary \
            + "\n\nAction Items:\n" + action_items
        newline = '\n'
        body_html = f"""
        <html>
            <body>
                <h4>Attendees</h4>
                <p>{attendance.replace(newline, '<br>')}</p>
                <h4>Summary</h4>
                <p>{summary.replace(newline, '<br>')}</p>
                <h4>Action Items</h4>
                <p>{action_items.replace(newline, '<br>')}</p>
            </body>
        </html>
        """

        attachment = MIMEApplication(transcript)
        attachment.add_header('Content-Disposition','attachment',filename="transcript.txt")
        msg.attach(attachment)

        attachment = MIMEApplication(chat)
        attachment.add_header('Content-Disposition','attachment',filename="chat.txt")
        msg.attach(attachment)

    charset = "utf-8"

    msg_body = MIMEMultipart('alternative')
    msg_body.attach(MIMEText(body_text.encode(charset), 'plain', charset))
    msg_body.attach(MIMEText(body_html.encode(charset), 'html', charset))
    msg.attach(msg_body)
    
    boto3.client("ses").send_raw_email(
        Source=email_source,
        Destinations=email_destinations,
        RawMessage={
            'Data':msg.as_string(),
        }
    )
    logging.info("Email sent!")

    exit()

async def initialize():

    start_command = "START"
    end_command = "END"

    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True, 
            ignore_default_args=['--mute-audio'],
            args=[
                "--window-size=1000,1000",
                "--use-fake-ui-for-media-stream",
                "--disable-notifications",
                "--disable-extensions",
                "--disable-crash-reporter",
                "--disable-dev-shm-usage",
                "--no-sandbox"
            ]
        )
        page = await browser.new_page()
        page.set_default_timeout(20000)

        logging.info("Getting meeting link.")
        logging.info(f"https://zoom.us/wc/{meeting_id.replace(' ', '')}/join")
        logging.info(meeting_password)
        await page.goto(f"https://zoom.us/wc/{meeting_id.replace(' ', '')}/join")

        logging.info("Typing meeting password.")
        password_text_element = await page.wait_for_selector('#input-for-pwd')
        await password_text_element.type(meeting_password)

        logging.info("Entering name.")
        name_text_element = await page.wait_for_selector('#input-for-name')
        await name_text_element.type(scribe_name)
        await name_text_element.press("Enter")

        logging.info("Adding audio.")
        audio_button_element = await page.wait_for_selector(
            "text=Join Audio by Computer",
            timeout=3000000
        )
        await audio_button_element.click()

        logging.info("Opening chat panel.")
        chat_button_element = await page.wait_for_selector(
            'button[aria-label^="open the chat panel"]'
        )
        await chat_button_element.hover()
        await chat_button_element.click()

        async def send_message(message):
            message_element = await page.wait_for_selector(
                'div[aria-placeholder="Type message here..."]'
            )
            await message_element.fill(message)
            await message_element.press('Enter')       

        logging.info("Sending introduction messages.")
        await send_message(
            'Hello! I am an AI-assisted scribe for Amazon Chime. To learn more about me,'
            ' visit https://github.com/aws-samples/automated-meeting-scribe-and-summarizer.'
        )
        await send_message(
            f'If all attendees consent, send "{start_command}" in the chat'
            ' to save attendance, new messages and transcriptions.'
        )
        await send_message(
            f'Otherwise, send "{end_command}" in the chat to remove me from this meeting.'
        )

        async def speaker_change(speaker):
            global current_speaker
            current_speaker = speaker
            if speaker not in attendees:
                attendees.append(speaker)
            logging.info('Speaker name changed:', speaker)

        await page.expose_function("speakerChange", speaker_change)

        async def start_transcription():
            logging.info('starting transcribe')
            await page.evaluate('''
                console.log("Hello there")
                const targetNode = document.querySelector(
                    '.speaker-active-container__video-frame .video-avatar__avatar .video-avatar__avatar-title'
                )
                const config = { childList: true, subtree: true }

                const callback = (mutationList, observer) => {
                    for (const mutation of mutationList) {
                        const speaker = mutation.target.textContent
                        if (speaker) {
                            speakerChange(speaker)
                        }
                    }
                }

                const observer = new MutationObserver(callback)
                observer.observe(targetNode, config)
            ''')
            global transcribe_task
            transcribe_task = asyncio.create_task(transcribe())

        async def message_change(message):
            logging.info(message)      
            global start
            if end_command in message:
                leave_button_element = await page.wait_for_selector('button[aria-label="Leave"]')
                await leave_button_element.hover()
                await leave_button_element.click()
            elif not start and start_command in message:
                start = True
                start_message = 'Saving attendance, new messages and transcriptions.'
                logging.info(start_message)
                send_start_meeting()
                await send_message(start_message)
                await start_transcription()
            elif start:
                messages.append(message)              

        await page.expose_function("messageChange", message_change)
        
        await page.evaluate('''
            const targetNode = document.querySelector('div[aria-label="Chat Message List"]')
            const config = { childList: true, subtree: true }

            const callback = (mutationList, observer) => {
                for (const mutation of mutationList) {
                    const addedNode = mutation.addedNodes[0]
                    if (addedNode) {
                        messageChange(
                            addedNode.querySelector('div[id^="chat-message-content"]').getAttribute('aria-label')
                        )  
                    }
                }
            }

            const observer = new MutationObserver(callback)
            observer.observe(targetNode, config)
        ''')

        async def meeting_end():
            global meeting_end
            try:
                done, pending = await asyncio.wait(
                    fs=[
                        asyncio.create_task(page.wait_for_selector('button[aria-label="Leave"]', state="detached", timeout=0)),
                        asyncio.create_task(page.wait_for_selector('div[class="zm-modal zm-modal-legacy"]', timeout=0))
                    ],
                    return_when=asyncio.FIRST_COMPLETED,
                    timeout=43200000
                )
                [task.cancel() for task in pending]
                logging.info("Meeting ended.")
            except:
                logging.info("Meeting timed out or something.")
            finally:
                send_end_meeting()
                meeting_end = True
        
        logging.info("Waiting for meeting end.")
        await meeting_end()
        await browser.close()
        if start:
            await transcribe_task

asyncio.run(initialize())


# deliver()