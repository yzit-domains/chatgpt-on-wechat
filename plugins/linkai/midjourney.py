from enum import Enum
from config import conf
from common.log import logger
import requests
import threading
import time
from bridge.reply import Reply, ReplyType
import aiohttp
import asyncio
from bridge.context import ContextType
from plugins import EventContext, EventAction


class TaskType(Enum):
    GENERATE = "generate"
    UPSCALE = "upscale"
    VARIATION = "variation"
    RESET = "reset"


class Status(Enum):
    PENDING = "pending"
    FINISHED = "finished"
    EXPIRED = "expired"
    ABORTED = "aborted"

    def __str__(self):
        return self.name


class MJTask:
    def __init__(self, id, user_id: str, task_type: TaskType, raw_prompt=None, expires: int=60*30, status=Status.PENDING):
        self.id = id
        self.user_id = user_id
        self.task_type = task_type
        self.raw_prompt = raw_prompt
        self.send_func = None  # send_func(img_url)
        self.expiry_time = time.time() + expires
        self.status = status
        self.img_url = None  # url
        self.img_id = None

    def __str__(self):
        return f"id={self.id}, user_id={self.user_id}, task_type={self.task_type}, status={self.status}, img_id={self.img_id}"

# midjourney bot
class MJBot:
    def __init__(self, config):
        self.base_url = "https://api.link-ai.chat/v1/img/midjourney"
        # self.base_url = "http://127.0.0.1:8911/v1/img/midjourney"
        self.headers = {"Authorization": "Bearer " + conf().get("linkai_api_key")}
        self.config = config
        self.tasks = {}
        self.temp_dict = {}
        self.tasks_lock = threading.Lock()
        self.event_loop = asyncio.new_event_loop()
        threading.Thread(name="mj-check-thread", target=self._run_loop, args=(self.event_loop,)).start()

    def judge_mj_task_type(self, e_context: EventContext) -> TaskType:
        """
        判断MJ任务的类型
        :param e_context: 上下文
        :return: 任务类型枚举
        """
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        context = e_context['context']
        if context.type == ContextType.TEXT:
            if self.config and self.config.get("enabled"):
                cmd_list = context.content.split(maxsplit=1)
                if cmd_list[0].lower() == f"{trigger_prefix}mj":
                    return TaskType.GENERATE
                elif cmd_list[0].lower() == f"{trigger_prefix}mju":
                    return TaskType.UPSCALE
                # elif cmd_list[0].lower() == f"{trigger_prefix}mjv":
                #     return TaskType.VARIATION
                # elif cmd_list[0].lower() == f"{trigger_prefix}mjr":
                #     return TaskType.RESET

    def process_mj_task(self, mj_type: TaskType, e_context: EventContext):
        """
        处理mj任务
        :param mj_type: mj任务类型
        :param e_context: 对话上下文
        """
        context = e_context['context']
        session_id = context["session_id"]
        cmd = context.content.split(maxsplit=1)
        if len(cmd) == 1:
            self._set_reply_text(self.get_help_text(verbose=True), e_context, level=ReplyType.ERROR)
            return

        if mj_type == TaskType.GENERATE:
            # 图片生成
            raw_prompt = cmd[1]
            reply = self.generate(raw_prompt, session_id, e_context)
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return

        elif mj_type == TaskType.UPSCALE:
            # 图片放大
            clist = cmd[1].split()
            if len(clist) < 2:
                self._set_reply_text(f"{cmd[0]} 命令缺少参数", e_context)
                return
            img_id = clist[0]
            index = int(clist[1])
            if index < 1 or index > 4:
                self._set_reply_text(f"图片序号 {index} 错误，应在 1 至 4 之间", e_context)
                return
            key = f"{TaskType.UPSCALE.name}_{img_id}_{index}"
            if self.temp_dict.get(key):
                self._set_reply_text(f"第 {index} 张图片已经放大过了", e_context)
                return
            # 图片放大操作
            reply = self.upscale(session_id, img_id, index, e_context)
            e_context['reply'] = reply
            e_context.action = EventAction.BREAK_PASS
            return

        else:
            self._set_reply_text(f"暂不支持该命令", e_context)

    def generate(self, prompt: str, user_id: str, e_context: EventContext) -> Reply:
        """
        图片生成
        :param prompt: 提示词
        :param user_id: 用户id
        :return: 任务ID
        """
        logger.info(f"[MJ] image generate, prompt={prompt}")
        body = {"prompt": prompt}
        res = requests.post(url=self.base_url + "/generate", json=body, headers=self.headers)
        if res.status_code == 200:
            res = res.json()
            logger.debug(f"[MJ] image generate, res={res}")
            if res.get("code") == 200:
                task_id = res.get("data").get("taskId")
                real_prompt = res.get("data").get("realPrompt")
                content = f"🚀你的作品将在1~2分钟左右完成，请耐心等待\n- - - - - - - - -\n"
                if real_prompt:
                    content += f"初始prompt: {prompt}\n转换后prompt: {real_prompt}"
                else:
                    content += f"prompt: {prompt}"
                reply = Reply(ReplyType.INFO, content)
                task = MJTask(id=task_id, status=Status.PENDING, raw_prompt=prompt, user_id=user_id, task_type=TaskType.GENERATE)
                # put to memory dict
                self.tasks[task.id] = task
                asyncio.run_coroutine_threadsafe(self.check_task(task, e_context), self.event_loop)
                return reply
        else:
            res_json = res.json()
            logger.error(f"[MJ] generate error, msg={res_json.get('message')}, status_code={res.status_code}")
            reply = Reply(ReplyType.ERROR, "图片生成失败，请稍后再试")
            return reply

    def upscale(self, user_id: str, img_id: str, index: int, e_context: EventContext) -> Reply:
        logger.info(f"[MJ] image upscale, img_id={img_id}, index={index}")
        body = {"type": TaskType.UPSCALE.name, "imgId": img_id, "index": index}
        res = requests.post(url=self.base_url + "/operate", json=body, headers=self.headers)
        if res.status_code == 200:
            res = res.json()
            logger.info(res)
            if res.get("code") == 200:
                task_id = res.get("data").get("taskId")
                content = f"🔎图片正在放大中，请耐心等待"
                reply = Reply(ReplyType.INFO, content)
                task = MJTask(id=task_id, status=Status.PENDING, user_id=user_id, task_type=TaskType.UPSCALE)
                # put to memory dict
                self.tasks[task.id] = task
                key = f"{TaskType.UPSCALE.name}_{img_id}_{index}"
                self.temp_dict[key] = True
                asyncio.run_coroutine_threadsafe(self.check_task(task, e_context), self.event_loop)
                return reply
        else:
            error_msg = ""
            if res.status_code == 461:
                error_msg = "请输入正确的图片ID"
            res_json = res.json()
            logger.error(f"[MJ] upscale error, msg={res_json.get('message')}, status_code={res.status_code}")
            reply = Reply(ReplyType.ERROR, error_msg or "图片生成失败，请稍后再试")
            return reply

    async def check_task(self, task: MJTask, e_context: EventContext):
        max_retry_time = 80
        while max_retry_time > 0:
            async with aiohttp.ClientSession() as session:
                url = f"{self.base_url}/tasks/{task.id}"
                async with session.get(url, headers=self.headers) as res:
                    if res.status == 200:
                        res_json = await res.json()
                        logger.debug(f"[MJ] task check res, task_id={task.id}, status={res.status}, "
                                     f"data={res_json.get('data')}, thread={threading.current_thread().name}")
                        if res_json.get("data") and res_json.get("data").get("status") == Status.FINISHED.name:
                            # process success res
                            self._process_success_task(task, res_json.get("data"), e_context)
                            return
                    else:
                        logger.warn(f"[MJ] image check error, status_code={res.status}")
                        max_retry_time -= 20
            await asyncio.sleep(10)
            max_retry_time -= 1
        logger.warn("[MJ] end from poll")

    def _process_success_task(self, task: MJTask, res: dict, e_context: EventContext):
        """
        处理任务成功的结果
        :param task: MJ任务
        :param res: 请求结果
        :param e_context: 对话上下文
        """
        # channel send img
        task.status = Status.FINISHED
        task.img_id = res.get("imgId")
        task.img_url = res.get("imgUrl")
        logger.info(f"[MJ] task success, task_id={task.id}, img_id={task.img_id}, img_url={task.img_url}")

        # send img
        reply = Reply(ReplyType.IMAGE_URL, task.img_url)
        channel = e_context["channel"]
        channel._send(reply, e_context["context"])

        # send info
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        text = ""
        if task.task_type == TaskType.GENERATE:
            text = f"🎨绘画完成!\nprompt: {task.raw_prompt}\n- - - - - - - - -\n图片ID: {task.img_id}"
            text += f"\n\n🔎可使用 {trigger_prefix}mju 命令放大指定图片\n"
            text += f"例如：\n{trigger_prefix}mju {task.img_id} 1"
            reply = Reply(ReplyType.INFO, text)
            channel._send(reply, e_context["context"])

        self._print_tasks()
        return

    def _run_loop(self, loop: asyncio.BaseEventLoop):
        loop.run_forever()
        loop.stop()

    def _print_tasks(self):
        for id in self.tasks:
            logger.debug(f"[MJ] current task: {self.tasks[id]}")


    def get_help_text(self, verbose=False, **kwargs):
        trigger_prefix = conf().get("plugin_trigger_prefix", "$")
        help_text = "利用midjourney来画图。\n"
        if not verbose:
            return help_text
        help_text += f"{trigger_prefix}mj 描述词1,描述词2 ... ： 利用描述词作画，参数请放在提示词之后。\n{trigger_prefix}mjimage 描述词1,描述词2 ... ： 利用描述词进行图生图，参数请放在提示词之后。\n{trigger_prefix}mjr ID: 对指定ID消息重新生成图片。\n{trigger_prefix}mju ID 图片序号: 对指定ID消息中的第x张图片进行放大。\n{trigger_prefix}mjv ID 图片序号: 对指定ID消息中的第x张图片进行变换。\n例如：\n\"{trigger_prefix}mj a little cat, white --ar 9:16\"\n\"{trigger_prefix}mjimage a white cat --ar 9:16\"\n\"{trigger_prefix}mju 1105592717188272288 2\""
        return help_text

    def _set_reply_text(self, content: str, e_context: EventContext, level: ReplyType=ReplyType.ERROR):
        reply = Reply(level, content)
        e_context["reply"] = reply
        e_context.action = EventAction.BREAK_PASS