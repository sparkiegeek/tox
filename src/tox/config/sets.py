from abc import ABC, abstractmethod
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Callable,
    Dict,
    Iterator,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Type,
    TypeVar,
    Union,
    cast,
)

from .loader.convert import Factory
from .loader.section import Section
from .of_type import ConfigConstantDefinition, ConfigDefinition, ConfigDynamicDefinition, ConfigLoadArgs
from .set_env import SetEnv
from .types import EnvList

if TYPE_CHECKING:
    from tox.config.loader.api import Loader
    from tox.config.main import Config

V = TypeVar("V")


class ConfigSet(ABC):
    """A set of configuration that belong together (such as a tox environment settings, core tox settings)"""

    def __init__(self, conf: "Config", section: Section, env_name: Optional[str]):
        self._section = section
        self._env_name = env_name
        self._conf = conf
        self.loaders: List[Loader[Any]] = []
        self._defined: Dict[str, ConfigDefinition[Any]] = {}
        self._keys: Dict[str, None] = {}
        self._alias: Dict[str, str] = {}
        self.register_config()

    @abstractmethod
    def register_config(self) -> None:
        raise NotImplementedError

    def add_config(
        self,
        keys: Union[str, Sequence[str]],
        of_type: Type[V],
        default: Union[Callable[["Config", Optional[str]], V], V],
        desc: str,
        post_process: Optional[Callable[[V], V]] = None,
        factory: Factory[V] = None,
    ) -> ConfigDynamicDefinition[V]:
        """
        Add configuration value.

        :param keys: the keys under what to register the config (first is primary key)
        :param of_type: the type of the config value
        :param default: the default value of the config value
        :param desc: a help message describing the configuration
        :param post_process: a callback to post-process the configuration value after it has been loaded
        :param factory: factory method to use to build the object
        :return: the new dynamic config definition
        """
        keys_ = self._make_keys(keys)
        definition = ConfigDynamicDefinition(keys_, desc, of_type, default, post_process, factory)
        result = self._add_conf(keys_, definition)
        return cast(ConfigDynamicDefinition[V], result)

    def add_constant(self, keys: Union[str, Sequence[str]], desc: str, value: V) -> ConfigConstantDefinition[V]:
        """
        Add a constant value.

        :param keys: the keys under what to register the config (first is primary key)
        :param desc: a help message describing the configuration
        :param value: the config value to use
        :return: the new constant config value
        """
        keys_ = self._make_keys(keys)
        definition = ConfigConstantDefinition(keys_, desc, value)
        result = self._add_conf(keys_, definition)
        return cast(ConfigConstantDefinition[V], result)

    @staticmethod
    def _make_keys(keys: Union[str, Sequence[str]]) -> Sequence[str]:
        return (keys,) if isinstance(keys, str) else keys

    def _add_conf(self, keys: Sequence[str], definition: ConfigDefinition[V]) -> ConfigDefinition[V]:
        key = keys[0]
        if key in self._defined:
            self._on_duplicate_conf(key, definition)
        else:
            self._keys[key] = None
            for item in keys:
                self._alias[item] = key
            for key in keys:
                self._defined[key] = definition
        return definition

    def _on_duplicate_conf(self, key: str, definition: ConfigDefinition[V]) -> None:
        earlier = self._defined[key]
        if definition != earlier:  # pragma: no branch
            raise ValueError(f"config {key} already defined")

    def __getitem__(self, item: str) -> Any:
        """
        Get the config value for a given key (will materialize in case of dynamic config).

        :param item: the config key
        :return: the configuration value
        """
        return self.load(item)

    def load(self, item: str, chain: Optional[List[str]] = None) -> Any:
        """
        Get the config value for a given key (will materialize in case of dynamic config).

        :param item: the config key
        :param chain: a chain of configuration keys already loaded for this load operation (used to detect circles)
        :return: the configuration value
        """
        config_definition = self._defined[item]
        return config_definition.__call__(self._conf, self.loaders, ConfigLoadArgs(chain, self.name, self.env_name))

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(loaders={self.loaders!r})"

    def __iter__(self) -> Iterator[str]:
        """:return: iterate through the defined config keys (primary keys used)"""
        return iter(self._keys.keys())

    def __contains__(self, item: str) -> bool:
        """
        Check if a configuration key is within the config set.

        :param item: the configuration value
        :return: a boolean indicating the truthiness of the statement
        """
        return item in self._alias

    def unused(self) -> List[str]:
        """:return: Return a list of keys present in the config source but not used"""
        found: Set[str] = set()
        # keys within loaders (only if the loader is not a parent too)
        parents = {id(i.parent) for i in self.loaders if i.parent is not None}
        for loader in self.loaders:
            if id(loader) not in parents:
                found.update(loader.found_keys())
        found -= self._defined.keys()
        return sorted(found)

    def primary_key(self, key: str) -> str:
        """
        Get the primary key for a config key.

        :param key: the config key
        :return: the key that's considered the primary for the input key
        """
        return self._alias[key]

    @property
    def name(self) -> str:
        return self._section.name

    @property
    def env_name(self) -> Optional[str]:
        return self._env_name


class CoreConfigSet(ConfigSet):
    """Configuration set for the core tox config"""

    def __init__(self, conf: "Config", section: Section, root: Path, src_path: Path) -> None:
        self._root = root
        self._src_path = src_path
        super().__init__(conf, section=section, env_name=None)

    def register_config(self) -> None:
        self.add_constant(keys=["config_file_path"], desc="path to the configuration file", value=self._src_path)
        self.add_config(
            keys=["tox_root", "toxinidir"],
            of_type=Path,
            default=self._root,
            desc="the root directory (where the configuration file is found)",
        )

        def work_dir_builder(conf: "Config", env_name: Optional[str]) -> Path:
            # here we pin to .tox/4 to be able to use in parallel with v3 until final release
            return (conf.work_dir if conf.work_dir is not None else cast(Path, self["tox_root"])) / ".tox" / "4"

        self.add_config(
            keys=["work_dir", "toxworkdir"],
            of_type=Path,
            default=work_dir_builder,
            desc="working directory",
        )
        self.add_config(
            keys=["temp_dir"],
            of_type=Path,
            default=lambda conf, _: cast(Path, self["tox_root"]) / ".temp",
            desc="temporary directory cleaned at start",
        )
        self.add_config(
            keys=["env_list", "envlist"],
            of_type=EnvList,
            default=EnvList([]),
            desc="define environments to automatically run",
        )

    def _on_duplicate_conf(self, key: str, definition: ConfigDefinition[V]) -> None:  # noqa: U100
        pass  # core definitions may be defined multiple times as long as all their options match, first defined wins


class EnvConfigSet(ConfigSet):
    """Configuration set for a tox environment"""

    def __init__(self, conf: "Config", section: Section, env_name: str) -> None:
        super().__init__(conf, section, env_name)
        self.default_set_env_loader: Callable[[], Mapping[str, str]] = lambda: {}

    def register_config(self) -> None:
        def set_env_post_process(values: SetEnv) -> SetEnv:
            values.update_if_not_present(self.default_set_env_loader())
            return values

        def set_env_factory(raw: object) -> SetEnv:
            if not isinstance(raw, str):
                raise TypeError(raw)
            return SetEnv(raw, self.name, self.env_name)

        self.add_config(
            keys=["set_env", "setenv"],
            of_type=SetEnv,
            factory=set_env_factory,
            default=SetEnv("", self.name, self.env_name),
            desc="environment variables to set when running commands in the tox environment",
            post_process=set_env_post_process,
        )

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}(name={self._env_name!r}, loaders={self.loaders!r})"


__all__ = (
    "ConfigSet",
    "CoreConfigSet",
    "EnvConfigSet",
)
