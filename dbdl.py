import gc 
import os
import ssl
import sys
import time
import trio
import uuid
import ujson
import shutil
import tarfile
import requests
import pandas as pd
from glob import glob
from uuid import uuid1
from io import BytesIO
from requests import get
from sqlalchemy import create_engine
from configparser import ConfigParser
from PIL import Image, ImageFile, UnidentifiedImageError 
from random_user_agent.user_agent import UserAgent
from random_user_agent.params import SoftwareName, OperatingSystem

sys.path.append('./crawlingathome-worker/')

import asks
asks.init("trio")

ImageFile.LOAD_TRUNCATED_IMAGES = True  # https://stackoverflow.com/a/47958486
ssl_ctx = ssl.create_default_context()
ssl_ctx.check_hostname = False
ssl_ctx.verify_mode = ssl.CERT_NONE

def config(filename='database.ini', section='postgresql'):
    # create a parser
    parser = ConfigParser()
    # read config file
    parser.read(filename)

    # get section, default to postgresql
    db = {}
    if parser.has_section(section):
        params = parser.items(section)
        for param in params:
            db[param[0]] = param[1]
    else:
        raise Exception('Section {0} not found in the {1} file'.format(section, filename))

    return db

class Tracer(trio.abc.Instrument):

    def __init__(self):
        self.exceptions = 0
        self.requests = 0
        self.downloads = 0
        self.imgproc_duration = 0
        self.download_duration = 0
        self.error_duration = 0

    def task_exited(self, task):
        if task.custom_sleep_data is not None:
            if task.custom_sleep_data[0] in [1, 3]: # this is exception
                self.exceptions += 1
                self.error_duration += task.custom_sleep_data[2]
            if task.custom_sleep_data[0] == 0: # this is image downloaded
                self.download_duration += task.custom_sleep_data[1]
                self.imgproc_duration += task.custom_sleep_data[2]
                self.downloads += 1

    def after_run(self):
        rate = round(self.exceptions / (self.exceptions + self.downloads + sys.float_info.epsilon), 2)
        avg_download = round(self.download_duration / (self.downloads + sys.float_info.epsilon), 2)
        avg_process = round(self.imgproc_duration / (self.downloads + sys.float_info.epsilon), 2)
        avg_error = round(self.error_duration / (self.exceptions + sys.float_info.epsilon), 2)
        print(f"[instrumentation] While scraping there were {self.exceptions} errors within {self.downloads + self.exceptions} candidates (error rate = {round(rate * 100,2)} %). {self.downloads} images were downloaded.")
        print(f"[instrumentation] Cumulative image processing duration {round(self.imgproc_duration, 2)} s.")
        print(f"[instrumentation] Average downloading time {avg_download} s/img, image processing time {avg_process} s/img, exceptions processing time {avg_error} s/link")

def log(e):
    with open("errors.txt","a") as f:
        f.write(str(e.__class__.__name__) + " " + str(e) + "\n")


def process_img_content(response, alt_text, license, sample_id):
    """
    Function to process downloaded image. Use use PIL from pillow-simd 
        (faster than open cv that in return is faster than original pillow)
    
    input: web request response, ALT text, license and sample id

    output: list of image parameters or None if image is rejected
    """
    img_output_folder = "save/images/"

    def _resize(im: Image):
        width, height = im.size
        ratio = min(width, height) / 224
        new_width = int(round(width/ratio,0))
        new_height = int(round(height/ratio,0))
        im = im.resize((new_width, new_height), resample=Image.BICUBIC)
        if new_width > 224 or new_height > 224:
            left = (new_width - 224)/2
            top = (new_height - 224)/2
            right = (new_width + 224)/2
            bottom = (new_height + 224)/2
            # Crop the center of the image
            im = im.crop((left, top, right, bottom))
        return im
    try:
        # reject too small images
        if len(response.content) < 5000:
            return
        img_data = BytesIO(response.content)
        with Image.open(img_data) as im:
            width, height = im.size
            # reject if too large (might be a DOS decompression bomb)
            if width * height > 89478484:
                return
            im_format = im.format
            out_fname = f"{img_output_folder}{str(sample_id)}.{im_format.lower()}"
            # reject if format is not in this list
            if im_format not in ["JPEG", "JPG", "PNG", "WEBP"]:
                return
            if min(width, height) > 224:
                im = _resize(im)
            
            # convert all images to RGB (necessary for CLIP, also CLIP is doing it again so do we need it here?)
            if im.mode != "RGB":
                im = im.convert("RGB")
            im.save(out_fname)
    except (KeyError, UnidentifiedImageError):
        return

    return [str(sample_id), out_fname, response.url, alt_text, width, height, license]


async def request_image(parsed_df):
    """
    This function initiates many parallel async connections to try download the images from provided links
    
    input: dataset of validated links, the sample id to start with

    output: list of lists with succesfully downloaded images and their parameters. this list is dumped on disk as json file
    """
    tmp_data = []
    limit = trio.CapacityLimiter(1000)

    # change the number of parallel connections based on CPU speed, network capabilities, etc.
    # the number of 192 is optimized for 1 vCPU droplet at Hetzner Cloud (code CX11)
    session = asks.Session(connections=64, ssl_context=ssl_ctx)

    software_names = [SoftwareName.CHROME.value]
    operating_systems = [OperatingSystem.LINUX.value]   

    user_agent_rotator = UserAgent(software_names=software_names, operating_systems=operating_systems, limit=2000)
    user_agent = user_agent_rotator.get_random_user_agent()

    # try to make the bot website friendly
    session.headers = {
        "User-Agent": user_agent,
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Referer": "https://google.com",
        "DNT": "1",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }

    async def _request(row):
        while True:
            start=time.time()
            sample_id = row[0]
            url = row[1]
            alt_text = row[2]
            license = row[3]
            # the following 2 lines are related to Trio Instrument to capture events from multiple threads
            task = trio.lowlevel.current_task()
            try:
                response = await session.get(url, timeout=10, connection_timeout=20)
                dltime = round(time.time()-start, 2)
                start=time.time()
                proces = process_img_content(
                    # tune timeout and connection_timeout to grab more or less files. shorter timeouts will exclude bad performing websites
                    response, alt_text, license, sample_id
                )
                proctime = round(time.time()-start, 2)
                task.custom_sleep_data = (0, dltime, proctime) # for success do not count errors
                if proces is not None:
                    tmp_data.append(proces)
            except Exception as e:
                log(e)
                task.custom_sleep_data = (1, 0, round(time.time()-start,2)) # when exception is hit, count it
            return

    async with trio.open_nursery() as n:
        for index, row in parsed_df.iterrows():
            async with limit:
                n.start_soon(_request, row)
            
    # trio makes sure at this point all async tasks were executed
    with open(f".tmp/{uuid1()}.json", "w") as f:
        ujson.dump(tmp_data, f)
    gc.collect()

    # add downloaded urls to parsed bloom server
    bloom2ip = "94.130.167.172"
    with open('hash.txt', 'w') as f:
        for url in parsed_df["url"]:
            f.write(url.strip()+"\n")
    post = {
        'file': ('hash.txt', open('hash.txt', 'rb')),
        'key': (None, 'parsed'),
    }
    
    failure = True
    for _ in range(10):
        try:
            response = requests.post(f'http://{bloom2ip}:8000/add/', files=post)
            if response.status_code != 200:
                print(f"bloom server error, retrying...")
                time.sleep(15)            
            else:
                failure = False
                break
        except:
            time.sleep(15)
    if failure:
        print(f"crash, cannot contact the parsed bloom server, please fix")

    return


def dl_wat(parsed_df): # replace valid data and start sampleid with parsed_df
    """
    This function initiates download attempt of validated parsed links
    It launches multithreaded tasks by using trio module
    
    input: dataset of validated links, the sample id to start with

    output: dataframe of downloaded images and their parameters
    """
    
    # Download every image available
    processed_samples = []
    #trio.run(request_image, valid_data, first_sample_id, instruments=[TrioProgress(len(valid_data), False)] )
    trio.run( request_image, parsed_df, instruments=[Tracer()] )

    for tmpf in glob(".tmp/*.json"):
        processed_samples.extend(ujson.load(open(tmpf)))
    return pd.DataFrame(
        processed_samples,
        columns=["SAMPLE_ID", "PATH", "URL", "TEXT", "HEIGHT", "WIDTH", "LICENSE"],
    )

def upload(source: str, clientType: str, target: str):
    with tarfile.open(f"{source}.tar.gz", "w:gz") as tar:
        tar.add(source, arcname=os.path.basename(source))
    print(f"client type is {clientType}")
    result = os.system(f"rsync -av {source}.tar.gz {target}")
    if os.path.exists(f"{source}.tar.gz"):
        os.remove(f"{source}.tar.gz")
    if os.path.exists(f"{source}"):
        shutil.rmtree(f"{source}", ignore_errors=True)
    return result

def newJob(engine):
    select_stmt1 = "UPDATE dataset SET status = 1 WHERE sampleid IN (SELECT DISTINCT ON (domain) sampleid FROM (SELECT domain, sampleid FROM dataset WHERE status = 0 LIMIT 50000 FOR UPDATE SKIP LOCKED) as \"U\" LIMIT 8000) AND status = 0 RETURNING sampleid"
    conn = engine.raw_connection()
    cur = conn.cursor()
    cur.execute(select_stmt1)
    result = cur.fetchall()
    conn.commit()
    cur.close()

    values = ",".join([str(tuple[0]) for tuple in result])
    select_stmt2 = "SELECT sampleid, url, text, license FROM dataset WHERE sampleid in ({})".format(values)
    df = pd.read_sql_query(select_stmt2, conn)
    conn.close()
    return df

def completeJob(engine, prefix, parsed_df, dlparse_df):
    values1 = ",".join(dlparse_df["SAMPLE_ID"].astype(str))
    values2 = ",".join(parsed_df["sampleid"].astype(str))
    update_stmt1 = "UPDATE dataset SET status=2, prefix='{}' where sampleid in ({})".format(prefix, values1)
    update_stmt2 = "UPDATE dataset SET status=9 where status=1 AND sampleid in ({})".format(values2)
    insert_stmt = "INSERT INTO jobs (jobid) VALUES ('{}')".format(prefix)

    if len(dlparse_df.index > 0):
        conn = engine.raw_connection()
        cur = conn.cursor()
        cur.execute(update_stmt1)
        cur.execute(insert_stmt)
        conn.commit()
        cur.close()
        conn.close()

    conn = engine.raw_connection()
    cur = conn.cursor()
    cur.execute(update_stmt2)
    conn.commit()
    cur.close()
    conn.close()
    return

if __name__ == "__main__":

    # initialize working folders
    output_folder = "./save/"
    img_output_folder = output_folder + "images/"

    print (f"starting session")
    
    params = config()
    engine = create_engine(f'postgresql://{params["user"]}:{params["password"]}@{params["host"]}:5432/{params["database"]}')

    while True:
        try:
            start = time.time()
            start0 = start

            parsed_df = newJob(engine)
            prefix = uuid.uuid4().hex
            result = 0

            # clear working folders for a new job
            if os.path.exists(output_folder):
                shutil.rmtree(output_folder, ignore_errors=True)
            if os.path.exists(".tmp"):
                shutil.rmtree(".tmp")

            os.mkdir(output_folder)
            os.mkdir(img_output_folder)
            os.mkdir(".tmp")

            # compute output file names base
            out_fname = f"3_staged_workflow_job_{prefix}_full_wat"
            print(f"[stats] Job acquired in {round(time.time()-start,2)} sec")
            start = time.time()

            print (f"[stats] This job has {len(parsed_df)} candidates")
        
            # attempt to download validated links and save to disk for stats and blocking lists
            dlparse_df = dl_wat(parsed_df)
            dlparse_df.to_csv(output_folder + out_fname + ".csv", index=False, sep="|")

            print (f"[stats] pairs retained {len(dlparse_df)} in {round(time.time() - start, 2)}")
            print (f"[stats] scraping efficiency {len(dlparse_df)/(time.time() - start)} img/sec")
            print (f"[stats] crawling efficiency {len(parsed_df)/(time.time() - start)} links/sec")

            # at this point we finishes the CPU node job, need to make the data available for GPU worker
            
            os.mkdir(prefix)
            os.system(f"mv save/* {prefix}/")
            result += upload(prefix, "CPU", "archiveteam@176.9.4.150::gpujobs") #todo find the IP and endpoint
            if result == 0:
                completeJob(engine, prefix, parsed_df, dlparse_df)

            last = round(time.time() - start0)

            print(f"[stats] Job completed in {last} seconds")
        
        except Exception as e:
            print (e)
            print ("Worker crashed")
            time.sleep(60)