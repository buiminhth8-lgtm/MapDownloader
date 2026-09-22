"""下载风险等级（Slice 23，Spec §2）。

四个等级，从"可以放心下"到"强烈不建议"：``NORMAL`` / ``LARGE`` /
``VERY_LARGE`` / ``DANGEROUS``。

为什么要有这一层而不是直接用一个数字
------------------------------------

因为**"多大算大"这件事会随硬件、网络与配额变化**，而"这个等级叫什么、
要不要确认、怎么在界面上标出来"是稳定的。把所有阈值收进
:mod:`app.planning.risk_policy`、把等级做成一个可比较的枚举，于是
"调阈值"改一个数字、"改措辞"改一处文案，两件事互不影响。

等级之间是**有序**的（:attr:`severity`），因此"取各维度最高等级"就是一次
``max()``。刻意用 :meth:`worst` 而不是重载 ``__lt__``：Python 的枚举默认
按**定义顺序**比较（``NORMAL < LARGE`` 恰好成立），但那是"碰巧对"——
换一个成员顺序就静默反过来。显式给一个 ``severity`` 才不怕重排。

``level`` 的负向表达很多（"不危险"、"不用确认"），因此**不提供** ``safe`` /
``ok`` 这类名字：读代码的人从名字上就该知道它说的是风险而不是状态。
"""

from __future__ import annotations

from enum import Enum

__all__ = [
    "RiskLevel",
    "RISK_LEVEL_ORDER",
    "RISK_LABEL",
]


class RiskLevel(Enum):
    """下载风险等级（Spec §2）。

    ``value`` 是**持久化 / 日志契约**（与 :class:`~app.planning.RegionLevel`
    同一规矩）：它会出现在日志行、执行记录与将来的报告里，改名等于打断下游。

    Attributes:
        NORMAL: 规模正常，直接开始。
        LARGE: 规模偏大，开始前确认一次。
        VERY_LARGE: 规模很大，给明确警告后确认。
        DANGEROUS: 规模危险（TB 级 / 数亿张），需要**强确认**。
    """

    NORMAL = "normal"
    LARGE = "large"
    VERY_LARGE = "very_large"
    DANGEROUS = "dangerous"

    # ------------------------------------------------------------------ #
    # 派生
    # ------------------------------------------------------------------ #
    @property
    def severity(self) -> int:
        """严重程度（``0`` ~ ``3``）：比较与"取最高"都靠它，不靠枚举顺序。"""
        return _SEVERITY[self]

    @property
    def display_name(self) -> str:
        """界面上那一行里的中文（``正常`` / ``较大`` / ``很大`` / ``危险``）。"""
        return _DISPLAY_NAMES[self]

    @property
    def symbol(self) -> str:
        """随等级变化的记号（Spec §11：不许只靠颜色表达风险）。

        ``正常`` 也给一个记号（而不是空串）：表格里那一列要么每行都有东西，
        要么整列看起来像"没算出来"。
        """
        return _SYMBOLS[self]

    @property
    def is_dangerous(self) -> bool:
        """是否达到"强烈不建议执行"的程度。"""
        return self is RiskLevel.DANGEROUS

    @property
    def is_normal(self) -> bool:
        """是否无需任何确认。"""
        return self is RiskLevel.NORMAL

    def at_least(self, other: "RiskLevel") -> bool:
        """本等级是否**不低于** ``other``。

        Raises:
            ValueError: ``other`` 不是 :class:`RiskLevel`。
        """
        return self.severity >= _require_level(other, "other").severity

    def worst(self, other: "RiskLevel") -> "RiskLevel":
        """两个等级里更严重的那个。"""
        return self if self.severity >= _require_level(other, "other").severity else other

    def label(self, *, with_value: bool = False) -> str:
        """展示用的等级文本。

        Args:
            with_value: 是否带上机器可读的名字（``危险（DANGEROUS）``）。
                日志与验收报告用 ``True``（可以 grep），界面用 ``False``。
                带上的是 ``name``（大写）而不是 ``value``（小写）：`value` 是
                持久化契约，而这里要的是"一眼看得出这是哪一个等级"。
        """
        if with_value:
            return f"{self.display_name}（{self.name}）"
        return self.display_name

    # ------------------------------------------------------------------ #
    # 构造
    # ------------------------------------------------------------------ #
    @classmethod
    def parse(cls, value: object) -> "RiskLevel":
        """把外部值（字符串 / 枚举）收敛成等级。

        认不出来一律退回 :attr:`NORMAL` 而不是抛异常：等级会出现在**旧版本
        写的**执行记录里，读不认识的等级不该让整份记录作废。真正的"数据坏了"
        由 store 层的字段校验负责。
        """
        if isinstance(value, cls):
            return value
        text = str(value or "").strip().lower()
        for item in cls:
            if item.value == text or item.name.lower() == text:
                return item
        return cls.NORMAL

    @classmethod
    def worst_of(cls, *levels: "RiskLevel") -> "RiskLevel":
        """一组等级里最严重的那个；空集合 → :attr:`NORMAL`。"""
        result = cls.NORMAL
        for item in levels:
            result = result.worst(item)
        return result


#: 从轻到重（用于遍历、报告排序与"取最高"）
RISK_LEVEL_ORDER: tuple[RiskLevel, ...] = (
    RiskLevel.NORMAL,
    RiskLevel.LARGE,
    RiskLevel.VERY_LARGE,
    RiskLevel.DANGEROUS,
)

#: 界面 / 日志里的固定前缀（与 :data:`~app.planning.models.ZOOM_RECOMMEND_LABEL`
#: 同一约定：文案只有一处，界面、日志、测试断言读到的必然是同一份）
RISK_LABEL: str = "风险："

_DISPLAY_NAMES: dict[RiskLevel, str] = {
    RiskLevel.NORMAL: "正常",
    RiskLevel.LARGE: "较大",
    RiskLevel.VERY_LARGE: "很大",
    RiskLevel.DANGEROUS: "危险",
}

_SYMBOLS: dict[RiskLevel, str] = {
    RiskLevel.NORMAL: "✓",
    RiskLevel.LARGE: "▲",
    RiskLevel.VERY_LARGE: "▲▲",
    RiskLevel.DANGEROUS: "✕",
}

#: 严重度名次。**不**直接用枚举的定义顺序：见模块文档。
_SEVERITY: dict[RiskLevel, int] = {
    level: index for index, level in enumerate(RISK_LEVEL_ORDER)
}


def _require_level(value: object, what: str) -> RiskLevel:
    """把参数收敛成等级，否则抛 ``ValueError``（带上是哪个参数）。"""
    if not isinstance(value, RiskLevel):
        raise ValueError(
            f"{what} 必须是 RiskLevel，实际为 {type(value).__name__}（{value!r}）"
        )
    return value
