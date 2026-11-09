"""识图引擎共用的失败类型。"""


class OcrUnavailable(RuntimeError):
    """本机没有可用的识别引擎。"""


class OcrFailed(RuntimeError):
    """引擎跑了，但没有得到可用文字。"""
