"""受控測試的持久進度：用訊息位置保留重複內容，不以文字雜湊去重。"""
from dataclasses import asdict
from datetime import datetime
import json
import os
from pathlib import Path

from chat_text import new_messages, parse_transcript


class ForwardProgress:
    def __init__(self, path: Path, context: dict, transcript: str):
        self.path, self.context = path, context
        self.senders = context['senders']
        self.skip_recalls = context['skip_recalls']
        if path.exists():
            self.state = json.loads(path.read_text(encoding='utf-8'))
            if self.state.get('version') != 1 or self.state.get('context') != context:
                raise ValueError('保存進度的群組、目的地或解析設定不同；保留紀錄，禁止猜測續傳。')
            if self.state.get('pending') is not None:
                raise ValueError('前次發送尚未確認；必須先查看手機與 log，禁止重送或覆蓋進度。')
            old = self.messages(self.state['transcript'])
            count = self.state.get('acknowledged_count')
            if type(count) is not int or not 0 <= count <= len(old):
                raise ValueError('保存的訊息位置無效；禁止續傳。')
            self.resumed = True
            # 只驗證，不先吃掉關機期間的新訊息。
            new_messages(self.state['transcript'], transcript, self.senders,
                         skip_recall_notices=self.skip_recalls)
        else:
            self.state = {'version': 1, 'context': context, 'transcript': transcript,
                          'acknowledged_count': len(self.messages(transcript)), 'pending': None}
            self.resumed = False
            self.save()

    def messages(self, transcript):
        return parse_transcript(transcript, self.senders, skip_recall_notices=self.skip_recalls)

    def save(self):
        self.state['time'] = datetime.now().astimezone().isoformat()
        temporary = self.path.with_suffix('.tmp')
        with temporary.open('w', encoding='utf-8') as handle:
            json.dump(self.state, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)

    def observe(self, transcript, notices=None):
        if self.state.get('pending') is not None:
            raise ValueError('發送結果尚未確認，禁止繼續讀取或發送。')
        new_messages(self.state['transcript'], transcript, self.senders,
                     skip_recall_notices=self.skip_recalls, notices=notices)
        messages = self.messages(transcript)
        if transcript != self.state['transcript']:
            self.state['transcript'] = transcript
            self.save()
        return messages[self.state['acknowledged_count']:]

    def begin(self, message, kind):
        if self.state.get('pending') is not None:
            raise ValueError('已有未確認發送，禁止再次嘗試。')
        index = self.state['acknowledged_count']
        messages = self.messages(self.state['transcript'])
        if index >= len(messages) or messages[index] != message or kind not in ('text', 'image'):
            raise ValueError('準備發送的訊息與保存位置不一致。')
        self.state['pending'] = {'index': index, 'message': asdict(message), 'kind': kind}
        self.save()  # 一定先保存，才可呼叫外部發送 API。

    def confirm(self, message_id):
        if (not self.state.get('pending') or type(message_id) is not int or message_id <= 0
                or self.state['pending']['index'] != self.state['acknowledged_count']):
            raise ValueError('成功回應與待確認位置不一致；不重送。')
        self.state['acknowledged_count'] += 1
        self.state.update(pending=None, last_message_id=message_id)
        self.save()

    def skip_sender(self, message):
        if self.state.get('pending') is not None:
            raise ValueError('已有待確認發送，禁止略過訊息。')
        index = self.state['acknowledged_count']
        messages = self.messages(self.state['transcript'])
        if (index >= len(messages) or messages[index] != message
                or message.sender not in self.context.get('ignored_senders', [])):
            raise ValueError('略過的發送者或訊息位置與保存設定不一致。')
        self.state['acknowledged_count'] += 1
        self.state['skipped_sender_count'] = self.state.get('skipped_sender_count', 0) + 1
        self.state['last_skipped_sender'] = {'day': message.day, 'clock': message.clock,
                                            'sender': message.sender, 'reason': 'configured_sender'}
        self.save()  # 沒有外部發送，仍保存位置，重啟不重新处理。
