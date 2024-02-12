import io
import os
import sys
import asyncio
import traceback

from fastapi.responses import FileResponse

import nodes
import folder_paths
import execution
import uuid
import urllib
import json
import glob
import struct
from PIL import Image, ImageOps
from PIL.PngImagePlugin import PngInfo
from io import BytesIO
import websocket
import uuid
import json
import urllib.request
import urllib.parse
# from script_examples.websockets_api_example import get_images
# from threading import Thread



try:
    import aiohttp
    from aiohttp import web
except ImportError:
    print("Module 'aiohttp' not installed. Please install it via:")
    print("pip install aiohttp")
    print("or")
    print("pip install -r requirements.txt")
    sys.exit()

import mimetypes
from comfy.cli_args import args
import comfy.utils
import comfy.model_management

from app.user_manager import UserManager

class BinaryEventTypes:
    PREVIEW_IMAGE = 1
    UNENCODED_PREVIEW_IMAGE = 2

async def send_socket_catch_exception(function, message):
    try:
        await function(message)
    except (aiohttp.ClientError, aiohttp.ClientPayloadError, ConnectionResetError) as err:
        print("send error:", err)

@web.middleware
async def cache_control(request: web.Request, handler):
    response: web.Response = await handler(request)
    print(request.path)
    if request.path.endswith('.js') or request.path.endswith('.css'):
        response.headers.setdefault('Cache-Control', 'no-cache')
    return response

def create_cors_middleware(allowed_origin: str):
    @web.middleware
    async def cors_middleware(request: web.Request, handler):
        if request.method == "OPTIONS":
            # Pre-flight request. Reply successfully:
            response = web.Response()
        else:
            response = await handler(request)
        print(allowed_origin)
        response.headers['Access-Control-Allow-Origin'] = "*"
        response.headers['Access-Control-Allow-Methods'] = '*'
        response.headers['Access-Control-Allow-Headers'] = '*'
        response.headers['Access-Control-Allow-Credentials'] = 'true'

        if request.method == "OPTIONS":
            # For preflight requests, return response with appropriate headers
            return web.Response(headers=response.headers)
        return response

    return cors_middleware




# def connect_ws(server_address,client_id):
#     while True:
#         try:
#             ws.connect("ws://{}/ws?clientId={}".format(server_address, client_id))
#             break
#         except:
#             pass

class PromptServer():
    def __init__(self, loop):
        PromptServer.instance = self

        mimetypes.init()
        mimetypes.types_map['.js'] = 'application/javascript; charset=utf-8'

        self.user_manager = UserManager()
        self.supports = ["custom_nodes_from_web"]
        self.prompt_queue = None
        self.loop = loop
        self.messages = asyncio.Queue()
        self.number = 0

        middlewares = [cache_control]
        print("aaa",args.enable_cors_header)
        # if args.enable_cors_header:
        middlewares.append(create_cors_middleware(args.enable_cors_header))

        max_upload_size = round(args.max_upload_size * 1024 * 1024)
        self.app = web.Application(client_max_size=max_upload_size, middlewares=middlewares)
        self.sockets = dict()
        self.web_root = os.path.join(os.path.dirname(
            os.path.realpath(__file__)), "web")
        routes = web.RouteTableDef()
        self.routes = routes
        self.last_node_id = None
        self.client_id = None

        self.on_prompt_handlers = []

        @routes.get('/ws')
        async def websocket_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            sid = request.rel_url.query.get('clientId', '')
            self.client_id = sid
            if sid:
                # Reusing existing session, remove old
                self.sockets.pop(sid, None)
            else:
                sid = uuid.uuid4().hex

            self.sockets[sid] = ws

            try:
                # Send initial state to the new client
                await self.send("status", { "status": self.get_queue_info(), 'sid': sid }, sid)
                # On reconnect if we are the currently executing client send the current node
                if self.client_id == sid and self.last_node_id is not None:
                    await self.send("executing", { "node": self.last_node_id }, sid)
                    
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.ERROR:
                        print('ws connection closed with exception %s' % ws.exception())
            finally:
                self.sockets.pop(sid, None)
            return ws

        @routes.get("/")
        async def get_root(request):
            return web.FileResponse(os.path.join(self.web_root, "index.html"))

        @routes.get("/embeddings")
        def get_embeddings(self):
            embeddings = folder_paths.get_filename_list("embeddings")
            return web.json_response(list(map(lambda a: os.path.splitext(a)[0], embeddings)))

        @routes.get("/extensions")
        async def get_extensions(request):
            files = glob.glob(os.path.join(
                glob.escape(self.web_root), 'extensions/**/*.js'), recursive=True)
            
            extensions = list(map(lambda f: "/" + os.path.relpath(f, self.web_root).replace("\\", "/"), files))
            
            for name, dir in nodes.EXTENSION_WEB_DIRS.items():
                files = glob.glob(os.path.join(glob.escape(dir), '**/*.js'), recursive=True)
                extensions.extend(list(map(lambda f: "/extensions/" + urllib.parse.quote(
                    name) + "/" + os.path.relpath(f, dir).replace("\\", "/"), files)))

            return web.json_response(extensions)

        def get_dir_by_type(dir_type):
            if dir_type is None:
                dir_type = "input"

            if dir_type == "input":
                type_dir = folder_paths.get_input_directory()
            elif dir_type == "temp":
                type_dir = folder_paths.get_temp_directory()
            elif dir_type == "output":
                type_dir = folder_paths.get_output_directory()

            return type_dir, dir_type

        def image_upload(post, image_save_function=None):
            image = post.get("image")
            overwrite = post.get("overwrite")

            image_upload_type = post.get("type")
            upload_dir, image_upload_type = get_dir_by_type(image_upload_type)

            if image and image.file:
                filename = image.filename
                if not filename:
                    return web.Response(status=400)

                subfolder = post.get("subfolder", "")
                full_output_folder = os.path.join(upload_dir, os.path.normpath(subfolder))
                filepath = os.path.abspath(os.path.join(full_output_folder, filename))

                if os.path.commonpath((upload_dir, filepath)) != upload_dir:
                    return web.Response(status=400)

                if not os.path.exists(full_output_folder):
                    os.makedirs(full_output_folder)

                split = os.path.splitext(filename)

                if overwrite is not None and (overwrite == "true" or overwrite == "1"):
                    pass
                else:
                    i = 1
                    while os.path.exists(filepath):
                        filename = f"{split[0]} ({i}){split[1]}"
                        filepath = os.path.join(full_output_folder, filename)
                        i += 1

                if image_save_function is not None:
                    image_save_function(image, post, filepath)
                else:
                    with open(filepath, "wb") as f:
                        f.write(image.file.read())

                return web.json_response({"name" : filename, "subfolder": subfolder, "type": image_upload_type})
            else:
                return web.Response(status=400)

        @routes.post("/upload/image")
        async def upload_image(request):
            post = await request.post()
            return image_upload(post)
        
        # @routes.post("/get-response")
        # async def get_response(request):
        #     server_address = "192.168.18.84:8188"
        #     client_id = str(uuid.uuid4())

        #     def queue_prompt(prompt):
        #         p = {"prompt": prompt, "client_id": client_id}
        #         data = json.dumps(p).encode('utf-8')
        #         req =  urllib.request.Request("http://{}/prompt".format(server_address), data=data)
        #         return json.loads(urllib.request.urlopen(req).read())

        #     def get_image(filename, subfolder, folder_type):
        #         data = {"filename": filename, "subfolder": subfolder, "type": folder_type}
        #         url_values = urllib.parse.urlencode(data)
        #         with urllib.request.urlopen("http://{}/view?{}".format(server_address, url_values)) as response:
        #             return response.read()

        #     def get_history(prompt_id):
        #         with urllib.request.urlopen("http://{}/history/{}".format(server_address, prompt_id)) as response:
        #             return json.loads(response.read())

        #     def get_images(ws, prompt):
        #         prompt_id = queue_prompt(prompt)['prompt_id']
        #         output_images = {}
        #         while True:
        #             out = ws.recv()
        #             if isinstance(out, str):
        #                 message = json.loads(out)
        #                 if message['type'] == 'executing':
        #                     data = message['data']
        #                     if data['node'] is None and data['prompt_id'] == prompt_id:
        #                         break #Execution is done
        #             else:
        #                 continue #previews are binary data

        #         history = get_history(prompt_id)[prompt_id]
        #         for o in history['outputs']:
        #             for node_id in history['outputs']:
        #                 node_output = history['outputs'][node_id]
        #                 if 'images' in node_output:
        #                     images_output = []
        #                     for image in node_output['images']:
        #                         image_data = get_image(image['filename'], image['subfolder'], image['type'])
        #                         images_output.append(image_data)
        #                 output_images[node_id] = images_output

        #         return output_images

           
        #     prompt_text = """
        #     {
        #     "prompt": {
        #         "3": {
        #         "inputs": {
        #             "seed": 618284639060744,
        #             "steps": 20,
        #             "cfg": 8,
        #             "sampler_name": "euler",
        #             "scheduler": "normal",
        #             "denoise": 1,
        #             "model": [
        #             "4",
        #             0
        #             ],
        #             "positive": [
        #             "6",
        #             0
        #             ],
        #             "negative": [
        #             "7",
        #             0
        #             ],
        #             "latent_image": [
        #             "16",
        #             0
        #             ]
        #         },
        #         "class_type": "KSampler"
        #         },
        #         "4": {
        #         "inputs": {
        #             "ckpt_name": "animagineXLV3_v30.safetensors"
        #         },
        #         "class_type": "CheckpointLoaderSimple"
        #         },
        #         "5": {
        #         "inputs": {
        #             "width": 512,
        #             "height": 512,
        #             "batch_size": 1
        #         },
        #         "class_type": "EmptyLatentImage"
        #         },
        #         "6": {
        #         "inputs": {
        #             "text": "change the background to blue and keep the front people the same",
        #             "clip": [
        #             "4",
        #             1
        #             ]
        #         },
        #         "class_type": "CLIPTextEncode"
        #         },
        #         "7": {
        #         "inputs": {
        #             "text": "text, watermark,person",
        #             "clip": [
        #             "4",
        #             1
        #             ]
        #         },
        #         "class_type": "CLIPTextEncode"
        #         },
        #         "9": {
        #         "inputs": {
        #             "filename_prefix": "ComfyUI",
        #             "images": [
        #             "14",
        #             0
        #             ]
        #         },
        #         "class_type": "SaveImage"
        #         },
        #         "10": {
        #         "inputs": {
        #             "image":"",
        #             "upload": "image"
        #         },
        #         "class_type": "LoadImage"
        #         },
        #         "14": {
        #         "inputs": {
        #             "samples": [
        #             "3",
        #             0
        #             ],
        #             "vae": [
        #             "4",
        #             2
        #             ]
        #         },
        #         "class_type": "VAEDecode"
        #         },
        #         "16": {
        #         "inputs": {
        #             "pixels": [
        #             "10",
        #             0
        #             ],
        #             "vae": [
        #             "4",
        #             2
        #             ]
        #         },
        #         "class_type": "VAEEncode"
        #         }
        #     },
        #     "extra_data": {
        #         "extra_pnginfo": {
        #         "workflow": {
        #             "last_node_id": 16,
        #             "last_link_id": 15,
        #             "nodes": [
        #             {
        #                 "id": 5,
        #                 "type": "EmptyLatentImage",
        #                 "pos": [
        #                 473,
        #                 609
        #                 ],
        #                 "size": {
        #                 "0": 315,
        #                 "1": 106
        #                 },
        #                 "flags": {},
        #                 "order": 0,
        #                 "mode": 0,
        #                 "outputs": [
        #                 {
        #                     "name": "LATENT",
        #                     "type": "LATENT",
        #                     "links": [],
        #                     "slot_index": 0
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "EmptyLatentImage"
        #                 },
        #                 "widgets_values": [
        #                 512,
        #                 512,
        #                 1
        #                 ]
        #             },
        #             {
        #                 "id": 9,
        #                 "type": "SaveImage",
        #                 "pos": [
        #                 1451,
        #                 189
        #                 ],
        #                 "size": {
        #                 "0": 210,
        #                 "1": 270
        #                 },
        #                 "flags": {},
        #                 "order": 8,
        #                 "mode": 0,
        #                 "inputs": [
        #                 {
        #                     "name": "images",
        #                     "type": "IMAGE",
        #                     "link": 12
        #                 }
        #                 ],
        #                 "properties": {},
        #                 "widgets_values": [
        #                 "ComfyUI"
        #                 ]
        #             },
        #             {
        #                 "id": 14,
        #                 "type": "VAEDecode",
        #                 "pos": [
        #                 1210,
        #                 188
        #                 ],
        #                 "size": {
        #                 "0": 210,
        #                 "1": 46
        #                 },
        #                 "flags": {},
        #                 "order": 7,
        #                 "mode": 0,
        #                 "inputs": [
        #                 {
        #                     "name": "samples",
        #                     "type": "LATENT",
        #                     "link": 10
        #                 },
        #                 {
        #                     "name": "vae",
        #                     "type": "VAE",
        #                     "link": 11
        #                 }
        #                 ],
        #                 "outputs": [
        #                 {
        #                     "name": "IMAGE",
        #                     "type": "IMAGE",
        #                     "links": [
        #                     12
        #                     ],
        #                     "shape": 3,
        #                     "slot_index": 0
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "VAEDecode"
        #                 }
        #             },
        #             {
        #                 "id": 10,
        #                 "type": "LoadImage",
        #                 "pos": [
        #                 867,
        #                 587
        #                 ],
        #                 "size": [
        #                 315,
        #                 313.9999694824219
        #                 ],
        #                 "flags": {},
        #                 "order": 1,
        #                 "mode": 0,
        #                 "outputs": [
        #                 {
        #                     "name": "IMAGE",
        #                     "type": "IMAGE",
        #                     "links": [
        #                     13
        #                     ],
        #                     "shape": 3,
        #                     "slot_index": 0
        #                 },
        #                 {
        #                     "name": "MASK",
        #                     "type": "MASK",
        #                     "links": "null",
        #                     "shape": 3
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "LoadImage"
        #                 },
        #                 "widgets_values": [
        #                 "3 idiots.jpg",
        #                 "image"
        #                 ]
        #             },
        #             {
        #                 "id": 4,
        #                 "type": "CheckpointLoaderSimple",
        #                 "pos": [
        #                 26,
        #                 474
        #                 ],
        #                 "size": {
        #                 "0": 315,
        #                 "1": 98
        #                 },
        #                 "flags": {},
        #                 "order": 2,
        #                 "mode": 0,
        #                 "outputs": [
        #                 {
        #                     "name": "MODEL",
        #                     "type": "MODEL",
        #                     "links": [
        #                     1
        #                     ],
        #                     "slot_index": 0
        #                 },
        #                 {
        #                     "name": "CLIP",
        #                     "type": "CLIP",
        #                     "links": [
        #                     3,
        #                     5
        #                     ],
        #                     "slot_index": 1
        #                 },
        #                 {
        #                     "name": "VAE",
        #                     "type": "VAE",
        #                     "links": [
        #                     11,
        #                     14
        #                     ],
        #                     "slot_index": 2
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "CheckpointLoaderSimple"
        #                 },
        #                 "widgets_values": [
        #                 "animagineXLV3_v30.safetensors"
        #                 ]
        #             },
        #             {
        #                 "id": 16,
        #                 "type": "VAEEncode",
        #                 "pos": [
        #                 1316,
        #                 665
        #                 ],
        #                 "size": {
        #                 "0": 210,
        #                 "1": 46
        #                 },
        #                 "flags": {},
        #                 "order": 5,
        #                 "mode": 0,
        #                 "inputs": [
        #                 {
        #                     "name": "pixels",
        #                     "type": "IMAGE",
        #                     "link": 13
        #                 },
        #                 {
        #                     "name": "vae",
        #                     "type": "VAE",
        #                     "link": 14
        #                 }
        #                 ],
        #                 "outputs": [
        #                 {
        #                     "name": "LATENT",
        #                     "type": "LATENT",
        #                     "links": [
        #                     15
        #                     ],
        #                     "shape": 3,
        #                     "slot_index": 0
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "VAEEncode"
        #                 }
        #             },
        #             {
        #                 "id": 3,
        #                 "type": "KSampler",
        #                 "pos": [
        #                 863,
        #                 186
        #                 ],
        #                 "size": {
        #                 "0": 315,
        #                 "1": 262
        #                 },
        #                 "flags": {},
        #                 "order": 6,
        #                 "mode": 0,
        #                 "inputs": [
        #                 {
        #                     "name": "model",
        #                     "type": "MODEL",
        #                     "link": 1
        #                 },
        #                 {
        #                     "name": "positive",
        #                     "type": "CONDITIONING",
        #                     "link": 4
        #                 },
        #                 {
        #                     "name": "negative",
        #                     "type": "CONDITIONING",
        #                     "link": 6
        #                 },
        #                 {
        #                     "name": "latent_image",
        #                     "type": "LATENT",
        #                     "link": 15
        #                 }
        #                 ],
        #                 "outputs": [
        #                 {
        #                     "name": "LATENT",
        #                     "type": "LATENT",
        #                     "links": [
        #                     10
        #                     ],
        #                     "slot_index": 0
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "KSampler"
        #                 },
        #                 "widgets_values": [
        #                 618284639060744,
        #                 "randomize",
        #                 20,
        #                 8,
        #                 "euler",
        #                 "normal",
        #                 1
        #                 ]
        #             },
        #             {
        #                 "id": 6,
        #                 "type": "CLIPTextEncode",
        #                 "pos": [
        #                 415,
        #                 186
        #                 ],
        #                 "size": {
        #                 "0": 422.84503173828125,
        #                 "1": 164.31304931640625
        #                 },
        #                 "flags": {},
        #                 "order": 3,
        #                 "mode": 0,
        #                 "inputs": [
        #                 {
        #                     "name": "clip",
        #                     "type": "CLIP",
        #                     "link": 3
        #                 }
        #                 ],
        #                 "outputs": [
        #                 {
        #                     "name": "CONDITIONING",
        #                     "type": "CONDITIONING",
        #                     "links": [
        #                     4
        #                     ],
        #                     "slot_index": 0
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "CLIPTextEncode"
        #                 },
        #                 "widgets_values": [
        #                 "change the background to blue and keep the front people the same"
        #                 ]
        #             },
        #             {
        #                 "id": 7,
        #                 "type": "CLIPTextEncode",
        #                 "pos": [
        #                 413,
        #                 389
        #                 ],
        #                 "size": {
        #                 "0": 425.27801513671875,
        #                 "1": 180.6060791015625
        #                 },
        #                 "flags": {},
        #                 "order": 4,
        #                 "mode": 0,
        #                 "inputs": [
        #                 {
        #                     "name": "clip",
        #                     "type": "CLIP",
        #                     "link": 5
        #                 }
        #                 ],
        #                 "outputs": [
        #                 {
        #                     "name": "CONDITIONING",
        #                     "type": "CONDITIONING",
        #                     "links": [
        #                     6
        #                     ],
        #                     "slot_index": 0
        #                 }
        #                 ],
        #                 "properties": {
        #                 "Node name for S&R": "CLIPTextEncode"
        #                 },
        #                 "widgets_values": [
        #                 "text, watermark,person"
        #                 ]
        #             }
        #             ],
        #             "links": [
        #             [
        #                 1,
        #                 4,
        #                 0,
        #                 3,
        #                 0,
        #                 "MODEL"
        #             ],
        #             [
        #                 3,
        #                 4,
        #                 1,
        #                 6,
        #                 0,
        #                 "CLIP"
        #             ],
        #             [
        #                 4,
        #                 6,
        #                 0,
        #                 3,
        #                 1,
        #                 "CONDITIONING"
        #             ],
        #             [
        #                 5,
        #                 4,
        #                 1,
        #                 7,
        #                 0,
        #                 "CLIP"
        #             ],
        #             [
        #                 6,
        #                 7,
        #                 0,
        #                 3,
        #                 2,
        #                 "CONDITIONING"
        #             ],
        #             [
        #                 10,
        #                 3,
        #                 0,
        #                 14,
        #                 0,
        #                 "LATENT"
        #             ],
        #             [
        #                 11,
        #                 4,
        #                 2,
        #                 14,
        #                 1,
        #                 "VAE"
        #             ],
        #             [
        #                 12,
        #                 14,
        #                 0,
        #                 9,
        #                 0,
        #                 "IMAGE"
        #             ],
        #             [
        #                 13,
        #                 10,
        #                 0,
        #                 16,
        #                 0,
        #                 "IMAGE"
        #             ],
        #             [
        #                 14,
        #                 4,
        #                 2,
        #                 16,
        #                 1,
        #                 "VAE"
        #             ],
        #             [
        #                 15,
        #                 16,
        #                 0,
        #                 3,
        #                 3,
        #                 "LATENT"
        #             ]
        #             ],
        #             "groups": [],
        #             "config": {},
        #             "extra": {},
        #             "version": 0.4
        #         }
        #         }
        #     }
        #     }
        #     """
        #     print(prompt_text)
        #     prompt = json.loads(prompt_text)
        #     post = await request.post()
        #     print("data",post.get('text'))
        #     print("image",post.get('image'))
        #     #set the text prompt for our positive CLIPTextEncode
        #     prompt["prompt"]["6"]["inputs"]["text"] = post.get("text")
        #     prompt['prompt']['10']['inputs']['image']=post.get("image")

        #     # #set the seed for our KSampler node
        #     # prompt["3"]["inputs"]["seed"] = 5
        #     # t1 = Thread(connect_ws,args=(server_address,client_id))
        #     # t1.start()
        #     print(client_id)
        #     print(server_address)
        #     ws = websocket.WebSocket()
        #     ws.connect("ws://{}/ws?clientId={}".format(server_address, client_id))
        #     print("WS Connected..")
        #     images = get_images(ws, prompt)
        #     for node_id in images:
        #         for image_data in images[node_id]:
        #             image = Image.open(io.BytesIO(image_data))

        #             # Provide the path where you want to save the image and specify the format (e.g., 'PNG', 'JPEG', 'GIF', etc.)
        #             save_path = f"output/{client_id}"  # Change the filename and format as needed
        #             image.save(save_path)

        #             print(f"Image saved to {save_path}")
        #         return save_path
        @routes.get("/get-image")
        async def get_image(request):
            return FileResponse( f"output/{request.rel_url.query['filename']}")


        @routes.post("/upload/mask")
        async def upload_mask(request):
            post = await request.post()

            def image_save_function(image, post, filepath):
                original_ref = json.loads(post.get("original_ref"))
                filename, output_dir = folder_paths.annotated_filepath(original_ref['filename'])

                # validation for security: prevent accessing arbitrary path
                if filename[0] == '/' or '..' in filename:
                    return web.Response(status=400)

                if output_dir is None:
                    type = original_ref.get("type", "output")
                    output_dir = folder_paths.get_directory_by_type(type)

                if output_dir is None:
                    return web.Response(status=400)

                if original_ref.get("subfolder", "") != "":
                    full_output_dir = os.path.join(output_dir, original_ref["subfolder"])
                    if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                        return web.Response(status=403)
                    output_dir = full_output_dir

                file = os.path.join(output_dir, filename)

                if os.path.isfile(file):
                    with Image.open(file) as original_pil:
                        metadata = PngInfo()
                        if hasattr(original_pil,'text'):
                            for key in original_pil.text:
                                metadata.add_text(key, original_pil.text[key])
                        original_pil = original_pil.convert('RGBA')
                        mask_pil = Image.open(image.file).convert('RGBA')

                        # alpha copy
                        new_alpha = mask_pil.getchannel('A')
                        original_pil.putalpha(new_alpha)
                        original_pil.save(filepath, compress_level=4, pnginfo=metadata)

            return image_upload(post, image_save_function)

        @routes.get("/view")
        async def view_image(request):
            if "filename" in request.rel_url.query:
                filename = request.rel_url.query["filename"]
                filename,output_dir = folder_paths.annotated_filepath(filename)

                # validation for security: prevent accessing arbitrary path
                if filename[0] == '/' or '..' in filename:
                    return web.Response(status=400)

                if output_dir is None:
                    type = request.rel_url.query.get("type", "output")
                    output_dir = folder_paths.get_directory_by_type(type)

                if output_dir is None:
                    return web.Response(status=400)

                if "subfolder" in request.rel_url.query:
                    full_output_dir = os.path.join(output_dir, request.rel_url.query["subfolder"])
                    if os.path.commonpath((os.path.abspath(full_output_dir), output_dir)) != output_dir:
                        return web.Response(status=403)
                    output_dir = full_output_dir

                filename = os.path.basename(filename)
                file = os.path.join(output_dir, filename)

                if os.path.isfile(file):
                    if 'preview' in request.rel_url.query:
                        with Image.open(file) as img:
                            preview_info = request.rel_url.query['preview'].split(';')
                            image_format = preview_info[0]
                            if image_format not in ['webp', 'jpeg'] or 'a' in request.rel_url.query.get('channel', ''):
                                image_format = 'webp'

                            quality = 90
                            if preview_info[-1].isdigit():
                                quality = int(preview_info[-1])

                            buffer = BytesIO()
                            if image_format in ['jpeg'] or request.rel_url.query.get('channel', '') == 'rgb':
                                img = img.convert("RGB")
                            img.save(buffer, format=image_format, quality=quality)
                            buffer.seek(0)

                            return web.Response(body=buffer.read(), content_type=f'image/{image_format}',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})

                    if 'channel' not in request.rel_url.query:
                        channel = 'rgba'
                    else:
                        channel = request.rel_url.query["channel"]

                    if channel == 'rgb':
                        with Image.open(file) as img:
                            if img.mode == "RGBA":
                                r, g, b, a = img.split()
                                new_img = Image.merge('RGB', (r, g, b))
                            else:
                                new_img = img.convert("RGB")

                            buffer = BytesIO()
                            new_img.save(buffer, format='PNG')
                            buffer.seek(0)

                            return web.Response(body=buffer.read(), content_type='image/png',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})

                    elif channel == 'a':
                        with Image.open(file) as img:
                            if img.mode == "RGBA":
                                _, _, _, a = img.split()
                            else:
                                a = Image.new('L', img.size, 255)

                            # alpha img
                            alpha_img = Image.new('RGBA', img.size)
                            alpha_img.putalpha(a)
                            alpha_buffer = BytesIO()
                            alpha_img.save(alpha_buffer, format='PNG')
                            alpha_buffer.seek(0)

                            return web.Response(body=alpha_buffer.read(), content_type='image/png',
                                                headers={"Content-Disposition": f"filename=\"{filename}\""})
                    else:
                        return web.FileResponse(file, headers={"Content-Disposition": f"filename=\"{filename}\""})

            return web.Response(status=404)

        @routes.get("/view_metadata/{folder_name}")
        async def view_metadata(request):
            folder_name = request.match_info.get("folder_name", None)
            if folder_name is None:
                return web.Response(status=404)
            if not "filename" in request.rel_url.query:
                return web.Response(status=404)

            filename = request.rel_url.query["filename"]
            if not filename.endswith(".safetensors"):
                return web.Response(status=404)

            safetensors_path = folder_paths.get_full_path(folder_name, filename)
            if safetensors_path is None:
                return web.Response(status=404)
            out = comfy.utils.safetensors_header(safetensors_path, max_size=1024*1024)
            if out is None:
                return web.Response(status=404)
            dt = json.loads(out)
            if not "__metadata__" in dt:
                return web.Response(status=404)
            return web.json_response(dt["__metadata__"])

        @routes.get("/system_stats")
        async def get_queue(request):
            device = comfy.model_management.get_torch_device()
            device_name = comfy.model_management.get_torch_device_name(device)
            vram_total, torch_vram_total = comfy.model_management.get_total_memory(device, torch_total_too=True)
            vram_free, torch_vram_free = comfy.model_management.get_free_memory(device, torch_free_too=True)
            system_stats = {
                "system": {
                    "os": os.name,
                    "python_version": sys.version,
                    "embedded_python": os.path.split(os.path.split(sys.executable)[0])[1] == "python_embeded"
                },
                "devices": [
                    {
                        "name": device_name,
                        "type": device.type,
                        "index": device.index,
                        "vram_total": vram_total,
                        "vram_free": vram_free,
                        "torch_vram_total": torch_vram_total,
                        "torch_vram_free": torch_vram_free,
                    }
                ]
            }
            return web.json_response(system_stats)

        @routes.get("/prompt")
        async def get_prompt(request):
            return web.json_response(self.get_queue_info())

        def node_info(node_class):
            obj_class = nodes.NODE_CLASS_MAPPINGS[node_class]
            info = {}
            info['input'] = obj_class.INPUT_TYPES()
            info['output'] = obj_class.RETURN_TYPES
            info['output_is_list'] = obj_class.OUTPUT_IS_LIST if hasattr(obj_class, 'OUTPUT_IS_LIST') else [False] * len(obj_class.RETURN_TYPES)
            info['output_name'] = obj_class.RETURN_NAMES if hasattr(obj_class, 'RETURN_NAMES') else info['output']
            info['name'] = node_class
            info['display_name'] = nodes.NODE_DISPLAY_NAME_MAPPINGS[node_class] if node_class in nodes.NODE_DISPLAY_NAME_MAPPINGS.keys() else node_class
            info['description'] = obj_class.DESCRIPTION if hasattr(obj_class,'DESCRIPTION') else ''
            info['category'] = 'sd'
            if hasattr(obj_class, 'OUTPUT_NODE') and obj_class.OUTPUT_NODE == True:
                info['output_node'] = True
            else:
                info['output_node'] = False

            if hasattr(obj_class, 'CATEGORY'):
                info['category'] = obj_class.CATEGORY
            return info

        @routes.get("/object_info")
        async def get_object_info(request):
            out = {}
            for x in nodes.NODE_CLASS_MAPPINGS:
                try:
                    out[x] = node_info(x)
                except Exception as e:
                    print(f"[ERROR] An error occurred while retrieving information for the '{x}' node.", file=sys.stderr)
                    traceback.print_exc()
            return web.json_response(out)

        @routes.get("/object_info/{node_class}")
        async def get_object_info_node(request):
            node_class = request.match_info.get("node_class", None)
            out = {}
            if (node_class is not None) and (node_class in nodes.NODE_CLASS_MAPPINGS):
                out[node_class] = node_info(node_class)
            return web.json_response(out)

        @routes.get("/history")
        async def get_history(request):
            max_items = request.rel_url.query.get("max_items", None)
            if max_items is not None:
                max_items = int(max_items)
            return web.json_response(self.prompt_queue.get_history(max_items=max_items))

        @routes.get("/history/{prompt_id}")
        async def get_history(request):
            prompt_id = request.match_info.get("prompt_id", None)
            return web.json_response(self.prompt_queue.get_history(prompt_id=prompt_id))

        @routes.get("/queue")
        async def get_queue(request):
            queue_info = {}
            current_queue = self.prompt_queue.get_current_queue()
            queue_info['queue_running'] = current_queue[0]
            queue_info['queue_pending'] = current_queue[1]
            return web.json_response(queue_info)

        @routes.post("/prompt")
        async def post_prompt(request):
            print("got prompt")
            resp_code = 200
            out_string = ""
            json_data =  await request.json()
            json_data = self.trigger_on_prompt(json_data)
            print(json_data)
            if "number" in json_data:
                number = float(json_data['number'])
            else:
                number = self.number
                if "front" in json_data:
                    if json_data['front']:
                        number = -number

                self.number += 1

            if "prompt" in json_data:
                prompt = json_data["prompt"]
                prompt=prompt['prompt']
                print("got prompt:", prompt)
                valid = execution.validate_prompt(prompt)
                extra_data = {}
                print("validate",valid)
                if "extra_data" in json_data:
                    extra_data = json_data["extra_data"]

                if "client_id" in json_data:
                    extra_data["client_id"] = json_data["client_id"]
                if valid[0]:
                    prompt_id = str(uuid.uuid4())
                    outputs_to_execute = valid[2]
                    self.prompt_queue.put((number, prompt_id, prompt, extra_data, outputs_to_execute))
                    response = {"prompt_id": prompt_id, "number": number, "node_errors": valid[3]}
                    return web.json_response(response)
                else:
                    print("invalid prompt:", valid[1])
                    return web.json_response({"error": valid[1], "node_errors": valid[3]}, status=400)
            else:
                return web.json_response({"error": "no prompt", "node_errors": []}, status=400)

        @routes.post("/queue")
        async def post_queue(request):
            json_data =  await request.json()
            if "clear" in json_data:
                if json_data["clear"]:
                    self.prompt_queue.wipe_queue()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    delete_func = lambda a: a[1] == id_to_delete
                    self.prompt_queue.delete_queue_item(delete_func)

            return web.Response(status=200)

        @routes.post("/interrupt")
        async def post_interrupt(request):
            nodes.interrupt_processing()
            return web.Response(status=200)
        

        @routes.get("/get-output-image")
        async def get_output_image(request):
            filename = os.listdir('output')
            return web.json_response(f"output/{filename[-2]}")



        @routes.post("/free")
        async def post_free(request):
            json_data = await request.json()
            unload_models = json_data.get("unload_models", False)
            free_memory = json_data.get("free_memory", False)
            if unload_models:
                self.prompt_queue.set_flag("unload_models", unload_models)
            if free_memory:
                self.prompt_queue.set_flag("free_memory", free_memory)
            return web.Response(status=200)

        @routes.post("/history")
        async def post_history(request):
            json_data =  await request.json()
            if "clear" in json_data:
                if json_data["clear"]:
                    self.prompt_queue.wipe_history()
            if "delete" in json_data:
                to_delete = json_data['delete']
                for id_to_delete in to_delete:
                    self.prompt_queue.delete_history_item(id_to_delete)

            return web.Response(status=200)
        
    def add_routes(self):
        self.user_manager.add_routes(self.routes)
        self.app.add_routes(self.routes)

        for name, dir in nodes.EXTENSION_WEB_DIRS.items():
            self.app.add_routes([
                web.static('/extensions/' + urllib.parse.quote(name), dir, follow_symlinks=True),
            ])

        self.app.add_routes([
            web.static('/', self.web_root, follow_symlinks=True),
        ])

    def get_queue_info(self):
        prompt_info = {}
        exec_info = {}
        exec_info['queue_remaining'] = self.prompt_queue.get_tasks_remaining()
        prompt_info['exec_info'] = exec_info
        return prompt_info

    async def send(self, event, data, sid=None):
        if event == BinaryEventTypes.UNENCODED_PREVIEW_IMAGE:
            await self.send_image(data, sid=sid)
        elif isinstance(data, (bytes, bytearray)):
            await self.send_bytes(event, data, sid)
        else:
            await self.send_json(event, data, sid)

    def encode_bytes(self, event, data):
        if not isinstance(event, int):
            raise RuntimeError(f"Binary event types must be integers, got {event}")

        packed = struct.pack(">I", event)
        message = bytearray(packed)
        message.extend(data)
        return message

    async def send_image(self, image_data, sid=None):
        image_type = image_data[0]
        image = image_data[1]
        max_size = image_data[2]
        if max_size is not None:
            if hasattr(Image, 'Resampling'):
                resampling = Image.Resampling.BILINEAR
            else:
                resampling = Image.ANTIALIAS

            image = ImageOps.contain(image, (max_size, max_size), resampling)
        type_num = 1
        if image_type == "JPEG":
            type_num = 1
        elif image_type == "PNG":
            type_num = 2

        bytesIO = BytesIO()
        header = struct.pack(">I", type_num)
        bytesIO.write(header)
        image.save(bytesIO, format=image_type, quality=95, compress_level=1)
        preview_bytes = bytesIO.getvalue()
        await self.send_bytes(BinaryEventTypes.PREVIEW_IMAGE, preview_bytes, sid=sid)

    async def send_bytes(self, event, data, sid=None):
        message = self.encode_bytes(event, data)

        if sid is None:
            sockets = list(self.sockets.values())
            for ws in sockets:
                await send_socket_catch_exception(ws.send_bytes, message)
        elif sid in self.sockets:
            await send_socket_catch_exception(self.sockets[sid].send_bytes, message)

    async def send_json(self, event, data, sid=None):
        message = {"type": event, "data": data}

        if sid is None:
            sockets = list(self.sockets.values())
            for ws in sockets:
                await send_socket_catch_exception(ws.send_json, message)
        elif sid in self.sockets:
            await send_socket_catch_exception(self.sockets[sid].send_json, message)

    def send_sync(self, event, data, sid=None):
        self.loop.call_soon_threadsafe(
            self.messages.put_nowait, (event, data, sid))

    def queue_updated(self):
        self.send_sync("status", { "status": self.get_queue_info() })

    async def publish_loop(self):
        while True:
            msg = await self.messages.get()
            await self.send(*msg)

    async def start(self, address, port, verbose=True, call_on_start=None):
        runner = web.AppRunner(self.app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, address, port)
        await site.start()

        if address == '':
            address = '0.0.0.0'
        if verbose:
            print("Starting server\n")
            print("To see the GUI go to: http://{}:{}".format(address, port))
        if call_on_start is not None:
            call_on_start(address, port)

    def add_on_prompt_handler(self, handler):
        self.on_prompt_handlers.append(handler)

    def trigger_on_prompt(self, json_data):
        for handler in self.on_prompt_handlers:
            try:
                json_data = handler(json_data)
            except Exception as e:
                print(f"[ERROR] An error occurred during the on_prompt_handler processing")
                traceback.print_exc()

        return json_data
