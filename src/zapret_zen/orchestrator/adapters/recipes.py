"""Desync recipe catalogue.

The catalogue is data, not code: each entry is a stable id plus the raw winws
arguments that implement it.  When Zapret 2 (winws2/Lua) support is added the
same shape can carry ``--lua-desync=...`` steps without changing the applier
or the UI.

Two transports are covered:

* ``tcp`` recipes desync the TLS ClientHello (the classic zapret profiles);
* ``udp`` recipes desync the QUIC Initial, which is a *separate* filter in
  winws (``--filter-udp=443``).  A domain can be perfectly healthy over TCP and
  still lose YouTube/Discord because the browser picked QUIC, so both halves
  are learned independently.

QUIC recipes reference the fake packet with the ``{bin}`` placeholder: only the
process that starts winws knows where its ``bin`` directory is, so the argument
is resolved at command-build time (see ``ProcessManager``).
"""

from __future__ import annotations

from zapret_zen.orchestrator.domain.models import Recipe

_QUIC_PATTERN = "{bin}/quic_initial_www_google_com.bin"

_CATALOG: tuple[Recipe, ...] = (
    Recipe(
        id="split2",
        title="Split2",
        args=("--dpi-desync=split2",),
        description="Классическое разбиение TLS ClientHello на два сегмента.",
    ),
    Recipe(
        id="split2-pos1",
        title="Split2 pos=1",
        args=("--dpi-desync=split2", "--dpi-desync-split-pos=1"),
        description="Разбиение сразу после первого байта SNI-окна.",
    ),
    Recipe(
        id="disorder",
        title="Disorder",
        args=("--dpi-desync=disorder",),
        description="Отправка сегментов в обратном порядке.",
    ),
    Recipe(
        id="disorder-pos1",
        title="Disorder pos=1",
        args=("--dpi-desync=disorder", "--dpi-desync-split-pos=1"),
        description="Disorder с фиксированной точкой разбиения.",
    ),
    Recipe(
        id="multidisorder",
        title="Multidisorder",
        args=("--dpi-desync=multidisorder", "--dpi-desync-split-pos=1"),
        description="Несколько переупорядоченных сегментов.",
    ),
    Recipe(
        id="multisplit",
        title="Multisplit",
        args=("--dpi-desync=multisplit", "--dpi-desync-split-pos=1"),
        description="Множественное разбиение с перекрытием.",
    ),
    Recipe(
        id="fakedsplit",
        title="Fake + split",
        args=(
            "--dpi-desync=fakedsplit",
            "--dpi-desync-split-pos=1",
            "--dpi-desync-fooling=ts",
            "--dpi-desync-fakedsplit-pattern=0x00",
        ),
        description="Поддельный пакет с пониженным TTL и разбиение.",
    ),
    Recipe(
        id="hostfakesplit",
        title="Host fake split",
        args=("--dpi-desync=hostfakesplit", "--dpi-desync-fooling=ts"),
        description="Подмена HTTP Host / SNI в поддельном пакете.",
    ),
    Recipe(
        id="syndata",
        title="SYN data",
        args=("--dpi-desync=syndata",),
        description="Данные в SYN-пакете.",
    ),
    Recipe(
        id="direct",
        title="Без десинка",
        args=(),
        description="Обход не требуется: домен исключается из автоподбора и не получает собственной секции.",
    ),
    # -- QUIC (UDP/443) ----------------------------------------------------
    Recipe(
        id="quic-fake2",
        title="QUIC fake x2",
        args=(
            "--dpi-desync=fake",
            "--dpi-desync-repeats=2",
            f"--dpi-desync-fake-quic={_QUIC_PATTERN}",
        ),
        description="Два поддельных QUIC Initial перед настоящим.",
        transport="udp",
    ),
    Recipe(
        id="quic-fake4",
        title="QUIC fake x4",
        args=(
            "--dpi-desync=fake",
            "--dpi-desync-repeats=4",
            f"--dpi-desync-fake-quic={_QUIC_PATTERN}",
        ),
        description="Четыре поддельных QUIC Initial: обходит DPI, режущий первый пакет.",
        transport="udp",
    ),
    Recipe(
        id="quic-fake6",
        title="QUIC fake x6",
        args=(
            "--dpi-desync=fake",
            "--dpi-desync-repeats=6",
            f"--dpi-desync-fake-quic={_QUIC_PATTERN}",
        ),
        description="Как в пресетах zapret: шесть повторов поддельного QUIC Initial.",
        transport="udp",
    ),
)

_TCP_ORDER = (
    "split2",
    "split2-pos1",
    "disorder",
    "disorder-pos1",
    "multidisorder",
    "multisplit",
    "fakedsplit",
    "hostfakesplit",
    "syndata",
)

_QUIC_ORDER = ("quic-fake2", "quic-fake4", "quic-fake6")


class RecipeCatalog:
    def __init__(self, extra: tuple[Recipe, ...] = ()) -> None:
        self._items: dict[str, Recipe] = {item.id: item for item in _CATALOG}
        for item in extra:
            self._items[item.id] = item

    def list_recipes(self) -> list[Recipe]:
        return list(self._items.values())

    def get(self, recipe_id: str) -> Recipe | None:
        return self._items.get(str(recipe_id or ""))

    def trial_order(self, transport: str = "tcp") -> list[Recipe]:
        """Recipes worth trying, most conservative first."""
        order = _QUIC_ORDER if str(transport).lower() == "udp" else _TCP_ORDER
        return [self._items[key] for key in order if key in self._items]

    def args_for(self, recipe_id: str) -> tuple[str, ...]:
        item = self.get(recipe_id)
        return item.args if item is not None else ()
