from __future__ import absolute_import
import re
import numpy as np
import warnings
from collections import OrderedDict
from copy import copy
from datetime import datetime
from six import with_metaclass, raise_from, text_type, binary_type, integer_types

from ..utils import docval, getargs, ExtenderMeta, get_docval, fmt_docval_args, call_docval_func
from ..container import Container, Data, DataRegion
from ..spec import Spec, AttributeSpec, DatasetSpec, GroupSpec, LinkSpec, NAME_WILDCARD, NamespaceCatalog, RefSpec,\
                   SpecReader
from ..data_utils import DataIO, AbstractDataChunkIterator
from ..spec.spec import BaseStorageSpec
from .builders import DatasetBuilder, GroupBuilder, LinkBuilder, Builder, ReferenceBuilder, RegionBuilder, BaseBuilder
from .warnings import OrphanContainerWarning, MissingRequiredWarning


class Proxy(object):
    """
    A temporary object to represent a Container. This gets used when resolving the true location of a
    Container's parent.

    Proxy objects allow simple bookkeeping of all potential parents a Container may have.

    This object is used by providing all the necessary information for describing the object. This object
    gets passed around and candidates are accumulated. Upon calling resolve, all saved candidates are matched
    against the information (provided to the constructor). The candidate that has an exact match is returned.
    """

    def __init__(self, manager, source, location, namespace, data_type):
        self.__source = source
        self.__location = location
        self.__namespace = namespace
        self.__data_type = data_type
        self.__manager = manager
        self.__candidates = list()

    @property
    def source(self):
        """The source of the object e.g. file source"""
        return self.__source

    @property
    def location(self):
        """The location of the object. This can be thought of as a unique path"""
        return self.__location

    @property
    def namespace(self):
        """The namespace from which the data_type of this Proxy came from"""
        return self.__namespace

    @property
    def data_type(self):
        """The data_type of Container that should match this Proxy"""
        return self.__data_type

    @docval({"name": "object", "type": (BaseBuilder, Container), "doc": "the container or builder to get a proxy for"})
    def matches(self, **kwargs):
        obj = getargs('object', kwargs)
        if not isinstance(obj, Proxy):
            obj = self.__manager.get_proxy(obj)
        return self == obj

    @docval({"name": "container", "type": Container, "doc": "the Container to add as a candidate match"})
    def add_candidate(self, **kwargs):
        container = getargs('container', kwargs)
        self.__candidates.append(container)

    def resolve(self, **kwargs):
        for candidate in self.__candidates:
            if self.matches(candidate):
                return candidate
        return None

    def __eq__(self, other):
        return self.data_type == other.data_type and \
               self.location == other.location and \
               self.namespace == other.namespace and \
               self.source == other.source

    def __repr__(self):
        ret = dict()
        for key in ('source', 'location', 'namespace', 'data_type'):
            ret[key] = getattr(self, key, None)
        return str(ret)


class BuildManager(object):
    """
    A class for managing builds of Containers
    """

    def __init__(self, type_map):
        self.__builders = dict()
        self.__containers = dict()
        self.__type_map = type_map

    @property
    def namespace_catalog(self):
        return self.__type_map.namespace_catalog

    @property
    def type_map(self):
        return self.__type_map

    @docval({"name": "object", "type": (BaseBuilder, Container), "doc": "the container or builder to get a proxy for"},
            {"name": "source", "type": str,
             "doc": "the source of container being built i.e. file path", 'default': None})
    def get_proxy(self, **kwargs):
        obj = getargs('object', kwargs)
        if isinstance(obj, BaseBuilder):
            return self.__get_proxy_builder(obj)
        elif isinstance(obj, Container):
            return self.__get_proxy_container(obj)

    def __get_proxy_builder(self, builder):
        dt = self.__type_map.get_builder_dt(builder)
        ns = self.__type_map.get_builder_ns(builder)
        stack = list()
        tmp = builder
        while tmp is not None:
            stack.append(tmp.name)
            tmp = self.__get_parent_dt_builder(tmp)
        loc = "/".join(reversed(stack))
        return Proxy(self, builder.source, loc, ns, dt)

    def __get_proxy_container(self, container):
        ns, dt = self.__type_map.get_container_ns_dt(container)
        stack = list()
        tmp = container
        while tmp is not None:
            if isinstance(tmp, Proxy):
                stack.append(tmp.location)
                break
            else:
                stack.append(tmp.name)
                tmp = tmp.parent
        loc = "/".join(reversed(stack))
        return Proxy(self, container.container_source, loc, ns, dt)

    @docval({"name": "container", "type": Container, "doc": "the container to convert to a Builder"},
            {"name": "source", "type": str,
             "doc": "the source of container being built i.e. file path", 'default': None})
    def build(self, **kwargs):
        """ Build the GroupBuilder for the given Container"""
        container = getargs('container', kwargs)
        container_id = self.__conthash__(container)
        result = self.__builders.get(container_id)
        source = getargs('source', kwargs)
        if result is None:
            if container.container_source is None:
                container.container_source = source
            else:
                if container.container_source != source:
                    raise ValueError("Can't change container_source once set")
            result = self.__type_map.build(container, self, source=source)
            self.prebuilt(container, result)
        elif container.modified:
            if isinstance(result, GroupBuilder):
                # TODO: if Datasets attributes are allowed to be modified, we need to
                # figure out how to handle that starting here.
                result = self.__type_map.build(container, self, builder=result, source=source)
        return result

    @docval({"name": "container", "type": Container, "doc": "the Container to save as prebuilt"},
            {'name': 'builder', 'type': (DatasetBuilder, GroupBuilder),
             'doc': 'the Builder representation of the given container'})
    def prebuilt(self, **kwargs):
        ''' Save the Builder for a given Container for future use '''
        container, builder = getargs('container', 'builder', kwargs)
        container_id = self.__conthash__(container)
        self.__builders[container_id] = builder
        builder_id = self.__bldrhash__(builder)
        self.__containers[builder_id] = container

    def __conthash__(self, obj):
        return id(obj)

    def __bldrhash__(self, obj):
        return id(obj)

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder),
             'doc': 'the builder to construct the Container from'})
    def construct(self, **kwargs):
        """ Construct the Container represented by the given builder """
        builder = getargs('builder', kwargs)
        if isinstance(builder, LinkBuilder):
            builder = builder.target
        builder_id = self.__bldrhash__(builder)
        result = self.__containers.get(builder_id)
        if result is None:
            result = self.__type_map.construct(builder, self)
            parent_builder = self.__get_parent_dt_builder(builder)
            if parent_builder is not None:
                result.parent = self.__get_proxy_builder(parent_builder)
            else:
                # we are at the top of the hierarchy,
                # so it must be time to resolve parents
                self.__resolve_parents(result)
            self.prebuilt(result, builder)
        result.set_modified(False)
        return result

    def __resolve_parents(self, container):
        stack = [container]
        while len(stack) > 0:
            tmp = stack.pop()
            if isinstance(tmp.parent, Proxy):
                tmp.parent = tmp.parent.resolve()
            for child in tmp.children:
                stack.append(child)

    def __get_parent_dt_builder(self, builder):
        '''
        Get the next builder above the given builder
        that has a data_type
        '''
        tmp = builder.parent
        ret = None
        while tmp is not None:
            try:
                ret = tmp
                self.__type_map.get_builder_dt(tmp)
                break
            except Exception:
                tmp = tmp.parent
        return ret

    @docval({'name': 'builder', 'type': Builder, 'doc': 'the Builder to get the class object for'})
    def get_cls(self, **kwargs):
        ''' Get the class object for the given Builder '''
        builder = getargs('builder', kwargs)
        return self.__type_map.get_cls(builder)

    @docval({"name": "container", "type": Container, "doc": "the container to convert to a Builder"},
            returns='The name a Builder should be given when building this container', rtype=str)
    def get_builder_name(self, **kwargs):
        ''' Get the name a Builder should be given '''
        container = getargs('container', kwargs)
        return self.__type_map.get_builder_name(container)

    @docval({'name': 'spec', 'type': (DatasetSpec, GroupSpec), 'doc': 'the parent spec to search'},
            {'name': 'builder', 'type': (DatasetBuilder, GroupBuilder, LinkBuilder),
             'doc': 'the builder to get the sub-specification for'})
    def get_subspec(self, **kwargs):
        '''
        Get the specification from this spec that corresponds to the given builder
        '''
        spec, builder = getargs('spec', 'builder', kwargs)
        return self.__type_map.get_subspec(spec, builder)

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder, LinkBuilder),
             'doc': 'the builder to get the sub-specification for'})
    def get_builder_ns(self, **kwargs):
        '''
        Get the namespace of a builder
        '''
        builder = getargs('builder', kwargs)
        return self.__type_map.get_builder_ns(builder)

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder, LinkBuilder),
             'doc': 'the builder to get the data_type for'})
    def get_builder_dt(self, **kwargs):
        '''
        Get the data_type of a builder
        '''
        builder = getargs('builder', kwargs)
        return self.__type_map.get_builder_dt(builder)


_const_arg = '__constructor_arg'


@docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
        is_method=False)
def _constructor_arg(**kwargs):
    '''Decorator to override the default mapping scheme for a given constructor argument.

    Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
    scheme for mapping between Container and Builder objects. The decorated method should accept as its
    first argument the Builder object that is being mapped. The method should return the value to be passed
    to the target Container class constructor argument given by *name*.
    '''
    name = getargs('name', kwargs)

    def _dec(func):
        setattr(func, _const_arg, name)
        return func
    return _dec


_obj_attr = '__object_attr'


@docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
        is_method=False)
def _object_attr(**kwargs):
    '''Decorator to override the default mapping scheme for a given object attribute.

    Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
    scheme for mapping between Container and Builder objects. The decorated method should accept as its
    first argument the Container object that is being mapped. The method should return the child Builder
    object (or scalar if the object attribute corresponds to an AttributeSpec) that represents the
    attribute given by *name*.
    '''
    name = getargs('name', kwargs)

    def _dec(func):
        setattr(func, _obj_attr, name)
        return func
    return _dec


def _unicode(s):
    """
    A helper function for converting to Unicode
    """
    if isinstance(s, text_type):
        return s
    elif isinstance(s, binary_type):
        return s.decode('utf-8')
    else:
        raise ValueError("Expected unicode or ascii string, got %s" % type(s))


def _ascii(s):
    """
    A helper function for converting to ASCII
    """
    if isinstance(s, text_type):
        return s.encode('ascii', 'backslashreplace')
    elif isinstance(s, binary_type):
        return s
    else:
        raise ValueError("Expected unicode or ascii string, got %s" % type(s))


class ObjectMapper(with_metaclass(ExtenderMeta, object)):
    '''A class for mapping between Spec objects and Container attributes

    '''

    __dtypes = {
        "float": np.float32,
        "float32": np.float32,
        "double": np.float64,
        "float64": np.float64,
        "long": np.int64,
        "int64": np.int64,
        "uint64": np.uint64,
        "int": np.int32,
        "int32": np.int32,
        "int16": np.int16,
        "int8": np.int8,
        "bool": np.bool_,
        "text": _unicode,
        "text": _unicode,
        "utf": _unicode,
        "utf8": _unicode,
        "utf-8": _unicode,
        "ascii": _ascii,
        "str": _ascii,
        "isodatetime": _ascii,
        "uint32": np.uint32,
        "uint16": np.uint16,
        "uint8": np.uint8,
    }

    @classmethod
    def __resolve_dtype(cls, given, specified):
        """
        Determine the dtype to use from the dtype of the given value and the specified dtype.
        This amounts to determining the greater precision of the two arguments, but also
        checks to make sure the same base dtype is being used.
        """
        g = np.dtype(given)
        s = np.dtype(specified)
        if g.itemsize <= s.itemsize:
            return s.type
        else:
            if g.name[:3] != s.name[:3]:    # different types
                if s.itemsize < 8:
                    msg = "expected %s, received %s - must supply %s or higher precision" % (s.name, g.name, s.name)
                else:
                    msg = "expected %s, received %s - must supply %s" % (s.name, g.name, s.name)
                raise ValueError(msg)
            else:
                return g.type

    @classmethod
    def convert_dtype(cls, spec, value):
        """
        Convert values to the specified dtype. For example, if a literal int
        is passed in to a field that is specified as a unsigned integer, this function
        will convert the Python int to a numpy unsigned int.

        :return: The function returns a tuple consisting of 1) the value, and 2) the data type.
                 The value is returned as the function may convert the input value to comply
                 with the dtype specified in the schema.
        """
        if value is None:
            dt = spec.dtype
            if isinstance(dt, RefSpec):
                dt = dt.reftype
            return None, dt
        if isinstance(value, DataIO):
            return value, cls.convert_dtype(spec, value.data)[1]
        if spec.dtype is None:
            return value, None
        if spec.dtype == 'numeric':
            return value, None
        if spec.dtype is not None and spec.dtype not in cls.__dtypes:
            msg = "unrecognized dtype: %s -- cannot convert value" % spec.dtype
            raise ValueError(msg)
        ret = None
        ret_dtype = None
        spec_dtype = cls.__dtypes[spec.dtype]
        if isinstance(value, np.ndarray):
            if spec_dtype is _unicode:
                ret = value.astype('U')
                ret_dtype = "utf8"
            elif spec_dtype is _ascii:
                ret = value.astype('S')
                ret_dtype = "ascii"
            else:
                dtype_func = cls.__resolve_dtype(value.dtype, spec_dtype)
                ret = value.astype(dtype_func)
                ret_dtype = ret.dtype.type
        elif isinstance(value, (tuple, list)):
            ret = list()
            for elem in value:
                tmp, tmp_dtype = cls.convert_dtype(spec, elem)
                ret.append(tmp)
            ret = type(value)(ret)
            ret_dtype = tmp_dtype
        else:
            if spec_dtype in (_unicode, _ascii):
                ret_dtype = 'ascii'
                if spec_dtype == _unicode:
                    ret_dtype = 'utf8'
                ret = spec_dtype(value)
            else:
                dtype_func = cls.__resolve_dtype(type(value), spec_dtype)
                ret = dtype_func(value)
                ret_dtype = type(ret)
        return ret, ret_dtype

    _const_arg = '__constructor_arg'

    @staticmethod
    @docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
            is_method=False)
    def constructor_arg(**kwargs):
        '''Decorator to override the default mapping scheme for a given constructor argument.

        Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
        scheme for mapping between Container and Builder objects. The decorated method should accept as its
        first argument the Builder object that is being mapped. The method should return the value to be passed
        to the target Container class constructor argument given by *name*.
        '''
        name = getargs('name', kwargs)
        return _constructor_arg(name)

    _obj_attr = '__object_attr'

    @staticmethod
    @docval({'name': 'name', 'type': str, 'doc': 'the name of the constructor argument'},
            is_method=False)
    def object_attr(**kwargs):
        '''Decorator to override the default mapping scheme for a given object attribute.

        Decorate ObjectMapper methods with this function when extending ObjectMapper to override the default
        scheme for mapping between Container and Builder objects. The decorated method should accept as its
        first argument the Container object that is being mapped. The method should return the child Builder
        object (or scalar if the object attribute corresponds to an AttributeSpec) that represents the
        attribute given by *name*.
        '''
        name = getargs('name', kwargs)
        return _object_attr(name)

    @staticmethod
    def __is_attr(attr_val):
        return hasattr(attr_val, _obj_attr)

    @staticmethod
    def __get_obj_attr(attr_val):
        return getattr(attr_val, _obj_attr)

    @staticmethod
    def __is_constructor_arg(attr_val):
        return hasattr(attr_val, _const_arg)

    @staticmethod
    def __get_cargname(attr_val):
        return getattr(attr_val, _const_arg)

    @ExtenderMeta.post_init
    def __gather_procedures(cls, name, bases, classdict):
        if hasattr(cls, 'constructor_args'):
            cls.constructor_args = copy(cls.constructor_args)
        else:
            cls.constructor_args = dict()
        if hasattr(cls, 'obj_attrs'):
            cls.obj_attrs = copy(cls.obj_attrs)
        else:
            cls.obj_attrs = dict()
        for name, func in cls.__dict__.items():
            if cls.__is_constructor_arg(func):
                cls.constructor_args[cls.__get_cargname(func)] = getattr(cls, name)
            elif cls.__is_attr(func):
                cls.obj_attrs[cls.__get_obj_attr(func)] = getattr(cls, name)

    @docval({'name': 'spec', 'type': (DatasetSpec, GroupSpec),
             'doc': 'The specification for mapping objects to builders'})
    def __init__(self, **kwargs):
        """ Create a map from Container attributes to NWB specifications """
        spec = getargs('spec', kwargs)
        self.__spec = spec
        self.__data_type_key = spec.type_key()
        self.__spec2attr = dict()
        self.__attr2spec = dict()
        self.__spec2carg = dict()
        self.__carg2spec = dict()
        self.__map_spec(spec)

    @property
    def spec(self):
        ''' the Spec used in this ObjectMapper '''
        return self.__spec

    @_constructor_arg('name')
    def get_container_name(self, *args):
        builder = args[0]
        return builder.name

    @classmethod
    @docval({'name': 'spec', 'type': Spec, 'doc': 'the specification to get the name for'})
    def convert_dt_name(cls, **kwargs):
        '''Get the attribute name corresponding to a specification'''
        spec = getargs('spec', kwargs)
        if spec.data_type_def is not None:
            name = spec.data_type_def
        elif spec.data_type_inc is not None:
            name = spec.data_type_inc
        else:
            raise ValueError('found spec without name or data_type')
        s1 = re.sub('(.)([A-Z][a-z]+)', r'\1_\2', name)
        name = re.sub('([a-z0-9])([A-Z])', r'\1_\2', s1).lower()
        if name[-1] != 's' and spec.is_many():
            name += 's'
        return name

    @classmethod
    def __get_fields(cls, name_stack, all_names, spec):
        name = spec.name
        if spec.name is None:
            name = cls.convert_dt_name(spec)
        name_stack.append(name)
        if name in all_names:
            name = "_".join(name_stack)
        all_names[name] = spec
        if isinstance(spec, BaseStorageSpec):
            if not (spec.data_type_def is None and spec.data_type_inc is None):
                # don't get names for components in data_types
                return
            for subspec in spec.attributes:
                cls.__get_fields(name_stack, all_names, subspec)
            if isinstance(spec, GroupSpec):
                for subspec in spec.datasets:
                    cls.__get_fields(name_stack, all_names, subspec)
                for subspec in spec.groups:
                    cls.__get_fields(name_stack, all_names, subspec)
                for subspec in spec.links:
                    cls.__get_fields(name_stack, all_names, subspec)
        name_stack.pop()

    @classmethod
    @docval({'name': 'spec', 'type': Spec, 'doc': 'the specification to get the object attribute names for'})
    def get_attr_names(cls, **kwargs):
        '''Get the attribute names for each subspecification in a Spec'''
        spec = getargs('spec', kwargs)
        names = OrderedDict()
        for subspec in spec.attributes:
            cls.__get_fields(list(), names, subspec)
        if isinstance(spec, GroupSpec):
            for subspec in spec.groups:
                cls.__get_fields(list(), names, subspec)
            for subspec in spec.datasets:
                cls.__get_fields(list(), names, subspec)
            for subspec in spec.links:
                cls.__get_fields(list(), names, subspec)
        return names

    def __map_spec(self, spec):
        attr_names = self.get_attr_names(spec)
        for k, v in attr_names.items():
            self.map_spec(k, v)

    @docval({"name": "attr_name", "type": str, "doc": "the name of the object to map"},
            {"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def map_attr(self, **kwargs):
        """ Map an attribute to spec. Use this to override default behavior """
        attr_name, spec = getargs('attr_name', 'spec', kwargs)
        if hasattr(spec, 'name') and spec.name is not None:
            n = spec.name
        elif hasattr(spec, 'data_type_def') and spec.data_type_def is not None:
            n = spec.data_type_def  # noqa: F841
        self.__spec2attr[spec] = attr_name
        self.__attr2spec[attr_name] = spec

    @docval({"name": "attr_name", "type": str, "doc": "the name of the attribute"})
    def get_attr_spec(self, **kwargs):
        """ Return the Spec for a given attribute """
        attr_name = getargs('attr_name', kwargs)
        return self.__attr2spec.get(attr_name)

    @docval({"name": "carg_name", "type": str, "doc": "the name of the constructor argument"})
    def get_carg_spec(self, **kwargs):
        """ Return the Spec for a given constructor argument """
        carg_name = getargs('carg_name', kwargs)
        return self.__attr2spec.get(carg_name)

    @docval({"name": "const_arg", "type": str, "doc": "the name of the constructor argument to map"},
            {"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def map_const_arg(self, **kwargs):
        """ Map an attribute to spec. Use this to override default behavior """
        const_arg, spec = getargs('const_arg', 'spec', kwargs)
        self.__spec2carg[spec] = const_arg
        self.__carg2spec[const_arg] = spec

    @docval({"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def unmap(self, **kwargs):
        """ Removing any mapping for a specification. Use this to override default mapping """
        spec = getargs('spec', kwargs)
        self.__spec2attr.pop(spec, None)
        self.__spec2carg.pop(spec, None)

    @docval({"name": "attr_carg", "type": str, "doc": "the constructor argument/object attribute to map this spec to"},
            {"name": "spec", "type": Spec, "doc": "the spec to map the attribute to"})
    def map_spec(self, **kwargs):
        """ Map the given specification to the construct argument and object attribute """
        spec, attr_carg = getargs('spec', 'attr_carg', kwargs)
        self.map_const_arg(attr_carg, spec)
        self.map_attr(attr_carg, spec)

    def __get_override_carg(self, *args):
        name = args[0]
        remaining_args = tuple(args[1:])
        if name in self.constructor_args:
            func = self.constructor_args[name]
            try:
                # remaining_args is [builder, manager]
                return func(self, *remaining_args)
            except TypeError:
                # LEGACY: remaining_args is [manager]
                return func(self, *remaining_args[:-1])
        return None

    def __get_override_attr(self, name, container, manager):
        if name in self.obj_attrs:
            func = self.obj_attrs[name]
            return func(self, container, manager)
        return None

    @docval({"name": "spec", "type": Spec, "doc": "the spec to get the attribute for"},
            returns='the attribute name', rtype=str)
    def get_attribute(self, **kwargs):
        ''' Get the object attribute name for the given Spec '''
        spec = getargs('spec', kwargs)
        val = self.__spec2attr.get(spec, None)
        return val

    @docval({"name": "spec", "type": Spec, "doc": "the spec to get the attribute value for"},
            {"name": "container", "type": Container, "doc": "the container to get the attribute value from"},
            {"name": "manager", "type": BuildManager, "doc": "the BuildManager used for managing this build"},
            returns='the value of the attribute')
    def get_attr_value(self, **kwargs):
        ''' Get the value of the attribute corresponding to this spec from the given container '''
        spec, container, manager = getargs('spec', 'container', 'manager', kwargs)
        attr_name = self.get_attribute(spec)
        if attr_name is None:
            return None
        attr_val = self.__get_override_attr(attr_name, container, manager)
        if attr_val is None:
            # TODO: A message like this should be used to warn users when an expected attribute
            # does not exist on a Container object
            #
            # if not hasattr(container, attr_name):
            #     msg = "Container '%s' (%s) does not have attribute '%s'" \
            #             % (container.name, type(container), attr_name)
            #     #warnings.warn(msg)
            attr_val = getattr(container, attr_name, None)
            if attr_val is not None:
                attr_val = self.__convert_value(attr_val, spec)
        return attr_val

    def __convert_value(self, value, spec):
        ret = value
        if isinstance(spec, AttributeSpec):
            if 'text' in spec.dtype:
                if spec.shape is not None:
                    ret = list(map(text_type, value))
                else:
                    ret = text_type(value)
        elif isinstance(spec, DatasetSpec):
            # TODO: make sure we can handle specs with data_type_inc set
            if spec.data_type_inc is not None:
                ret = value
            else:
                if spec.dtype is not None:
                    string_type = None
                    if 'text' in spec.dtype:
                        string_type = text_type
                    elif 'ascii' in spec.dtype:
                        string_type = binary_type
                    elif 'isodatetime' in spec.dtype:
                        string_type = datetime.isoformat
                    if string_type is not None:
                        if spec.dims is not None:
                            ret = list(map(string_type, value))
                        else:
                            ret = string_type(value)
        return ret

    @docval({"name": "spec", "type": Spec, "doc": "the spec to get the constructor argument for"},
            returns="the name of the constructor argument", rtype=str)
    def get_const_arg(self, **kwargs):
        ''' Get the constructor argument for the given Spec '''
        spec = getargs('spec', kwargs)
        return self.__spec2carg.get(spec, None)

    @docval({"name": "container", "type": Container, "doc": "the container to convert to a Builder"},
            {"name": "manager", "type": BuildManager, "doc": "the BuildManager to use for managing this build"},
            {"name": "parent", "type": Builder, "doc": "the parent of the resulting Builder", 'default': None},
            {"name": "source", "type": str,
             "doc": "the source of container being built i.e. file path", 'default': None},
            {"name": "builder", "type": GroupBuilder, "doc": "the Builder to build on", 'default': None},
            {"name": "spec_ext", "type": Spec, "doc": "a spec extension", 'default': None},
            returns="the Builder representing the given Container", rtype=Builder)
    def build(self, **kwargs):
        ''' Convert a Container to a Builder representation '''
        container, manager, parent, source = getargs('container', 'manager', 'parent', 'source', kwargs)
        builder = getargs('builder', kwargs)
        name = manager.get_builder_name(container)
        if isinstance(self.__spec, GroupSpec):
            if builder is None:
                builder = GroupBuilder(name, parent=parent, source=source)
            self.__add_datasets(builder, self.__spec.datasets, container, manager, source)
            self.__add_groups(builder, self.__spec.groups, container, manager, source)
            self.__add_links(builder, self.__spec.links, container, manager, source)
        else:
            if not isinstance(container, Data):
                msg = "'container' must be of type Data with DatasetSpec"
                raise ValueError(msg)
            if isinstance(self.spec.dtype, RefSpec):
                bldr_data = self.__get_ref_builder(self.spec.dtype, self.spec.shape, container, manager)
                try:
                    bldr_data, dtype = self.convert_dtype(self.spec, bldr_data)
                except Exception as ex:
                    msg = 'could not resolve dtype for %s \'%s\'' % (type(container).__name__, container.name)
                    raise_from(Exception(msg), ex)
                builder = DatasetBuilder(name, bldr_data, parent=parent, source=source, dtype=dtype)
            elif isinstance(self.spec.dtype, list):
                refs = [(i, subt) for i, subt in enumerate(self.spec.dtype) if isinstance(subt.dtype, RefSpec)]
                bldr_data = copy(container.data)
                bldr_data = list()
                for i, row in enumerate(container.data):
                    tmp = list(row)
                    for j, subt in refs:
                        tmp[j] = self.__get_ref_builder(subt.dtype, None, row[j], manager)
                    bldr_data.append(tuple(tmp))
                try:
                    bldr_data, dtype = self.convert_dtype(self.spec, bldr_data)
                except Exception as ex:
                    msg = 'could not resolve dtype for %s \'%s\'' % (type(container).__name__, container.name)
                    raise_from(Exception(msg), ex)
                builder = DatasetBuilder(name, bldr_data, parent=parent, source=source, dtype=dtype)
            else:
                if self.__spec.dtype is None and self.__is_reftype(container.data):
                    bldr_data = list()
                    for d in container.data:
                        bldr_data.append(ReferenceBuilder(manager.build(d)))
                    builder = DatasetBuilder(name, bldr_data, parent=parent, source=source,
                                             dtype='object')
                else:
                    try:
                        bldr_data, dtype = self.convert_dtype(self.spec, container.data)
                    except Exception as ex:
                        msg = 'could not resolve dtype for %s \'%s\'' % (type(container).__name__, container.name)
                        raise_from(Exception(msg), ex)
                    builder = DatasetBuilder(name, bldr_data, parent=parent, source=source, dtype=dtype)
        self.__add_attributes(builder, self.__spec.attributes, container, manager, source)
        return builder

    def __is_reftype(self, data):
        tmp = data
        while hasattr(tmp, '__len__') and not isinstance(tmp, (Container, text_type, binary_type)):
            tmptmp = None
            for t in tmp:
                # In case of a numeric array stop the iteration at the first element to avoid long-running loop
                if isinstance(t, (integer_types, float, complex, bool)):
                    break
                if hasattr(t, '__len__') and not isinstance(t, (Container, text_type, binary_type)) and len(t) > 0:
                    tmptmp = tmp[0]
                    break
            if tmptmp is not None:
                break
            else:
                tmp = tmp[0]
        if isinstance(tmp, Container):
            return True
        else:
            return False

    def __get_ref_builder(self, dtype, shape, container, manager):
        bldr_data = None
        if dtype.is_region():
            if shape is None:
                if not isinstance(container, DataRegion):
                    msg = "'container' must be of type DataRegion if spec represents region reference"
                    raise ValueError(msg)
                bldr_data = RegionBuilder(container.region, manager.build(container.data))
            else:
                bldr_data = list()
                for d in container.data:
                    bldr_data.append(RegionBuilder(d.slice, manager.build(d.target)))
        else:
            if shape is None:
                if isinstance(container, Container):
                    bldr_data = ReferenceBuilder(manager.build(container))
                else:
                    bldr_data = ReferenceBuilder(manager.build(container.data))
            else:
                bldr_data = list()
                for d in container.data:
                    bldr_data.append(ReferenceBuilder(manager.build(d.target)))
        return bldr_data

    def __is_null(self, item):
        if item is None:
            return True
        else:
            if any(isinstance(item, t) for t in (list, tuple, dict, set)):
                return len(item) == 0
        return False

    def __add_attributes(self, builder, attributes, container, build_manager, source):
        for spec in attributes:
            if spec.value is not None:
                attr_value = spec.value
            else:
                attr_value = self.get_attr_value(spec, container, build_manager)
                if attr_value is None:
                    attr_value = spec.default_value

            if isinstance(spec.dtype, RefSpec):
                if not self.__is_reftype(attr_value):
                    if attr_value is None:
                        msg = "object of data_type %s not found on %s '%s'" % \
                              (spec.dtype.target_type, type(container).__name__, container.name)
                    else:
                        msg = "invalid type for reference '%s' (%s) - must be Container" % (spec.name, type(attr_value))
                    raise ValueError(msg)
                target_builder = build_manager.build(attr_value, source=source)
                attr_value = ReferenceBuilder(target_builder)
            else:
                if attr_value is not None:
                    try:
                        attr_value, attr_dtype = self.convert_dtype(spec, attr_value)
                    except Exception as ex:
                        msg = 'could not convert %s for %s %s' % (spec.name, type(container).__name__, container.name)
                        raise_from(Exception(msg), ex)

            # do not write empty or null valued objects
            if attr_value is None:
                if spec.required:
                    msg = "attribute '%s' for '%s' (%s)"\
                                  % (spec.name, builder.name, self.spec.data_type_def)
                    warnings.warn(msg, MissingRequiredWarning)
                continue

            builder.set_attribute(spec.name, attr_value)

    def __add_links(self, builder, links, container, build_manager, source):
        for spec in links:
            attr_value = self.get_attr_value(spec, container, build_manager)
            if not attr_value:
                continue
            self.__add_containers(builder, spec, attr_value, build_manager, source, container)

    def __is_empty(self, val):
        if val is None:
            return True
        if isinstance(val, DataIO):
            val = val.data
        if isinstance(val, AbstractDataChunkIterator):
            return False
        else:
            if (hasattr(val, '__len__') and len(val) == 0):
                return True
            else:
                return False

    def __add_datasets(self, builder, datasets, container, build_manager, source):
        for spec in datasets:
            attr_value = self.get_attr_value(spec, container, build_manager)
            # TODO: add check for required datasets
            if self.__is_empty(attr_value):
                if spec.required:
                    msg = "dataset '%s' for '%s' of type (%s)"\
                                  % (spec.name, builder.name, self.spec.data_type_def)
                    warnings.warn(msg, MissingRequiredWarning)
                continue
            if spec.data_type_def is None and spec.data_type_inc is None:
                if spec.name in builder.datasets:
                    sub_builder = builder.datasets[spec.name]
                else:
                    try:
                        data, dtype = self.convert_dtype(spec, attr_value)
                    except Exception as ex:
                        msg = 'could not convert \'%s\' for %s \'%s\''
                        msg = msg % (spec.name, type(container).__name__, container.name)
                        raise_from(Exception(msg), ex)
                    sub_builder = builder.add_dataset(spec.name, data, dtype=dtype)
                self.__add_attributes(sub_builder, spec.attributes, container, build_manager, source)
            else:
                self.__add_containers(builder, spec, attr_value, build_manager, source, container)

    def __add_groups(self, builder, groups, container, build_manager, source):
        for spec in groups:
            if spec.data_type_def is None and spec.data_type_inc is None:
                # we don't need to get attr_name since any named
                # group does not have the concept of value
                sub_builder = builder.groups.get(spec.name)
                if sub_builder is None:
                    sub_builder = GroupBuilder(spec.name, source=source)
                self.__add_attributes(sub_builder, spec.attributes, container, build_manager, source)
                self.__add_datasets(sub_builder, spec.datasets, container, build_manager, source)

                # handle subgroups that are not Containers
                attr_name = self.get_attribute(spec)
                if attr_name is not None:
                    attr_value = getattr(container, attr_name, None)
                    attr_value = self.get_attr_value(spec, container, build_manager)
                    if any(isinstance(attr_value, t) for t in (list, tuple, set, dict)):
                        it = iter(attr_value)
                        if isinstance(attr_value, dict):
                            it = iter(attr_value.values())
                        for item in it:
                            if isinstance(item, Container):
                                self.__add_containers(sub_builder, spec, item, build_manager, source, container)
                self.__add_groups(sub_builder, spec.groups, container, build_manager, source)
                empty = sub_builder.is_empty()
                if not empty or (empty and isinstance(spec.quantity, int)):
                    if sub_builder.name not in builder.groups:
                        builder.set_group(sub_builder)
            else:
                if spec.data_type_def is not None:
                    attr_name = self.get_attribute(spec)
                    if attr_name is not None:
                        attr_value = getattr(container, attr_name, None)
                        if attr_value is not None:
                            self.__add_containers(builder, spec, attr_value, build_manager, source, container)
                else:
                    attr_name = self.get_attribute(spec)
                    attr_value = getattr(container, attr_name, None)
                    if attr_value is not None:
                        self.__add_containers(builder, spec, attr_value, build_manager, source, container)

    def __add_containers(self, builder, spec, value, build_manager, source, parent_container):
        if isinstance(value, Container):
            if value.parent is None:
                msg = "'%s' (%s) for '%s' (%s)"\
                              % (value.name, getattr(value, self.spec.type_key()),
                                 builder.name, self.spec.data_type_def)
                warnings.warn(msg, OrphanContainerWarning)
            if value.modified:                   # writing a new container
                rendered_obj = build_manager.build(value, source=source)
                # use spec to determine what kind of HDF5
                # object this Container corresponds to
                if isinstance(spec, LinkSpec) or value.parent is not parent_container:
                    name = spec.name
                    builder.set_link(LinkBuilder(rendered_obj, name, builder))
                elif isinstance(spec, DatasetSpec):
                    if rendered_obj.dtype is None and spec.dtype is not None:
                        val, dtype = self.convert_dtype(spec, None)
                        rendered_obj.dtype = dtype
                    builder.set_dataset(rendered_obj)
                else:
                    builder.set_group(rendered_obj)
            elif value.container_source:        # make a link to an existing container
                if value.container_source != parent_container.container_source or\
                   value.parent is not parent_container:
                    rendered_obj = build_manager.build(value, source=source)
                    builder.set_link(LinkBuilder(rendered_obj, name=spec.name, parent=builder))
            else:
                raise ValueError("Found unmodified Container with no source - '%s' with parent '%s'" %
                                 (value.name, parent_container.name))
        else:
            if any(isinstance(value, t) for t in (list, tuple)):
                values = value
            elif isinstance(value, dict):
                values = value.values()
            else:
                msg = ("received %s, expected Container - 'value' "
                       "must be an Container a list/tuple/dict of "
                       "Containers if 'spec' is a GroupSpec")
                raise ValueError(msg % value.__class__.__name__)
            for container in values:
                if container:
                    self.__add_containers(builder, spec, container, build_manager, source, parent_container)

    def __get_subspec_values(self, builder, spec, manager):
        ret = dict()
        # First get attributes
        attributes = builder.attributes
        for attr_spec in spec.attributes:
            attr_val = attributes.get(attr_spec.name)
            if attr_val is None:
                continue
            if isinstance(attr_val, (GroupBuilder, DatasetBuilder)):
                ret[attr_spec] = manager.construct(attr_val)
            elif isinstance(attr_val, RegionBuilder):
                raise ValueError("RegionReferences as attributes is not yet supported")
            elif isinstance(attr_val, ReferenceBuilder):
                ret[attr_spec] = manager.construct(attr_val.builder)
            else:
                ret[attr_spec] = attr_val
        if isinstance(spec, GroupSpec):
            if not isinstance(builder, GroupBuilder):
                raise ValueError("__get_subspec_values - must pass GroupBuilder with GroupSpec")
            # first aggregate links by data type and separate them
            # by group and dataset
            groups = dict(builder.groups)             # make a copy so we can separate links
            datasets = dict(builder.datasets)         # make a copy so we can separate links
            links = builder.links
            link_dt = dict()
            for link_builder in links.values():
                target = link_builder.builder
                if isinstance(target, DatasetBuilder):
                    datasets[link_builder.name] = target
                else:
                    groups[link_builder.name] = target
                dt = manager.get_builder_dt(target)
                if dt is not None:
                    link_dt.setdefault(dt, list()).append(target)
            # now assign links to their respective specification
            for subspec in spec.links:
                if subspec.name is not None:
                    ret[subspec] = manager.construct(links[subspec.name].builder)
                else:
                    sub_builder = link_dt.get(subspec.target_type)
                    if sub_builder is not None:
                        ret[subspec] = self.__flatten(sub_builder, subspec, manager)
            # now process groups and datasets
            self.__get_sub_builders(groups, spec.groups, manager, ret)
            self.__get_sub_builders(datasets, spec.datasets, manager, ret)
        elif isinstance(spec, DatasetSpec):
            if not isinstance(builder, DatasetBuilder):
                raise ValueError("__get_subspec_values - must pass DatasetBuilder with DatasetSpec")
            ret[spec] = builder.data
        return ret

    def __get_sub_builders(self, sub_builders, subspecs, manager, ret):
        # index builders by data_type
        builder_dt = dict()
        for g in sub_builders.values():
            try:
                dt = manager.get_builder_dt(g)
                ns = manager.get_builder_ns(g)
            except ValueError:
                continue
            if dt is not None:
                for parent_dt in manager.namespace_catalog.get_hierarchy(ns, dt):
                    builder_dt.setdefault(parent_dt, list()).append(g)
        for subspec in subspecs:
            # first get data type for the spec
            if subspec.data_type_def is not None:
                dt = subspec.data_type_def
            elif subspec.data_type_inc is not None:
                dt = subspec.data_type_inc
            else:
                dt = None
            # use name if we can, otherwise use data_data
            if subspec.name is None:
                sub_builder = builder_dt.get(dt)
                if sub_builder is not None:
                    sub_builder = self.__flatten(sub_builder, subspec, manager)
                    ret[subspec] = sub_builder
            else:
                sub_builder = sub_builders.get(subspec.name)
                if sub_builder is None:
                    continue
                if dt is None:
                    # recurse
                    ret.update(self.__get_subspec_values(sub_builder, subspec, manager))
                else:
                    ret[subspec] = manager.construct(sub_builder)

    def __flatten(self, sub_builder, subspec, manager):
        tmp = [manager.construct(b) for b in sub_builder]
        if len(tmp) == 1 and not subspec.is_many():
            tmp = tmp[0]
        return tmp

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder),
             'doc': 'the builder to construct the Container from'},
            {'name': 'manager', 'type': BuildManager, 'doc': 'the BuildManager for this build'})
    def construct(self, **kwargs):
        ''' Construct an Container from the given Builder '''
        builder, manager = getargs('builder', 'manager', kwargs)
        cls = manager.get_cls(builder)
        # gather all subspecs
        subspecs = self.__get_subspec_values(builder, self.spec, manager)
        # get the constructor argument that each specification corresponds to
        const_args = dict()
        for subspec, value in subspecs.items():
            const_arg = self.get_const_arg(subspec)
            if const_arg is not None:
                if isinstance(subspec, BaseStorageSpec) and subspec.is_many():
                    existing_value = const_args.get(const_arg)
                    if isinstance(existing_value, list):
                        value = existing_value + value
                const_args[const_arg] = value
        # build kwargs for the constructor
        kwargs = dict()
        for const_arg in get_docval(cls.__init__):
            argname = const_arg['name']
            override = self.__get_override_carg(argname, builder, manager)
            if override is not None:
                val = override
            elif argname in const_args:
                val = const_args[argname]
            else:
                continue
            kwargs[argname] = val
        try:
            obj = cls(**kwargs)
            obj.container_source = builder.source
        except Exception as ex:
            msg = 'Could not construct %s object' % (cls.__name__,)
            raise_from(Exception(msg), ex)
        return obj

    @docval({'name': 'container', 'type': Container, 'doc': 'the Container to get the Builder name for'})
    def get_builder_name(self, **kwargs):
        '''Get the name of a Builder that represents a Container'''
        container = getargs('container', kwargs)
        if self.__spec.name not in (NAME_WILDCARD, None):
            ret = self.__spec.name
        else:
            if container.name is None:
                if self.__spec.default_name is not None:
                    ret = self.__spec.default_name
                else:
                    msg = 'Unable to determine name of container type %s' % self.__spec.data_type_def
                    raise ValueError(msg)
            else:
                ret = container.name
        return ret


class TypeSource(object):
    '''A class to indicate the source of a data_type in a namespace.

    This class should only be used by TypeMap
    '''

    @docval({"name": "namespace", "type": str, "doc": "the namespace the from, which the data_type originated"},
            {"name": "data_type", "type": str, "doc": "the name of the type"})
    def __init__(self, **kwargs):
        namespace, data_type = getargs('namespace', 'data_type', kwargs)
        self.__namespace = namespace
        self.__data_type = data_type

    @property
    def namespace(self):
        return self.__namespace

    @property
    def data_type(self):
        return self.__data_type


class TypeMap(object):
    ''' A class to maintain the map between ObjectMappers and Container classes
    '''

    @docval({'name': 'namespaces', 'type': NamespaceCatalog, 'doc': 'the NamespaceCatalog to use'},
            {'name': 'mapper_cls', 'type': type, 'doc': 'the ObjectMapper class to use', 'default': ObjectMapper})
    def __init__(self, **kwargs):
        namespaces = getargs('namespaces', kwargs)
        self.__ns_catalog = namespaces
        self.__mappers = dict()     # already constructed ObjectMapper classes
        self.__mapper_cls = dict()  # the ObjectMapper class to use for each container type
        self.__container_types = OrderedDict()
        self.__data_types = dict()
        self.__default_mapper_cls = getargs('mapper_cls', kwargs)

    @property
    def namespace_catalog(self):
        return self.__ns_catalog

    def __copy__(self):
        ret = TypeMap(copy(self.__ns_catalog), self.__default_mapper_cls)
        ret.merge(self)
        return ret

    def __deepcopy__(self, memo):
        # XXX: From @nicain: All of a sudden legacy tests started
        #      needing this argument in deepcopy. Doesn't hurt anything, though.
        return self.__copy__()

    def copy_mappers(self, type_map):
        for namespace in self.__ns_catalog.namespaces:
            if namespace not in type_map.__container_types:
                continue
            for data_type in self.__ns_catalog.get_namespace(namespace).get_registered_types():
                container_cls = type_map.__container_types[namespace].get(data_type)
                if container_cls is None:
                    continue
                self.register_container_type(namespace, data_type, container_cls)
                if container_cls in type_map.__mapper_cls:
                    self.register_map(container_cls, type_map.__mapper_cls[container_cls])

    def merge(self, type_map):
        for namespace in type_map.__container_types:
            for data_type in type_map.__container_types[namespace]:

                container_cls = type_map.__container_types[namespace][data_type]
                self.register_container_type(namespace, data_type, container_cls)

        for container_cls in type_map.__mapper_cls:
            self.register_map(container_cls, type_map.__mapper_cls[container_cls])

    @docval({'name': 'namespace_path', 'type': str, 'doc': 'the path to the file containing the namespaces(s) to load'},
            {'name': 'resolve', 'type': bool,
             'doc': 'whether or not to include objects from included/parent spec objects', 'default': True},
            {'name': 'reader',
             'type': SpecReader,
             'doc': 'the class to user for reading specifications', 'default': None},
            returns="the namespaces loaded from the given file", rtype=tuple)
    def load_namespaces(self, **kwargs):
        '''Load namespaces from a namespace file.

        This method will call load_namespaces on the NamespaceCatalog used to construct this TypeMap. Additionally,
        it will process the return value to keep track of what types were included in the loaded namespaces. Calling
        load_namespaces here has the advantage of being able to keep track of type dependencies across namespaces.
        '''
        deps = call_docval_func(self.__ns_catalog.load_namespaces, kwargs)
        for new_ns, ns_deps in deps.items():
            for src_ns, types in ns_deps.items():
                for dt in types:
                    container_cls = self.get_container_cls(src_ns, dt)
                    if container_cls is None:
                        container_cls = TypeSource(src_ns, dt)
                    self.register_container_type(new_ns, dt, container_cls)
        return tuple(deps.keys())

    _type_map = {
        'text': str,
        'float': float,
        'float64': float,
        'int': int,
        'int32': int,
        'isodatetime': datetime
    }

    def __get_type(self, spec):
        if isinstance(spec, AttributeSpec):
            if isinstance(spec.dtype, RefSpec):
                tgttype = spec.dtype.target_type
                for val in self.__container_types.values():
                    container_type = val.get(tgttype)
                    if container_type is not None:
                        return container_type
                return (Data, Container)
            elif spec.shape is None:
                return self._type_map.get(spec.dtype)
            else:
                return ('array_data',)
        elif isinstance(spec, LinkSpec):
            return Container
        else:
            if not (spec.data_type_inc is None and spec.data_type_inc is None):
                if spec.name is not None:
                    return (list, tuple, dict, set)
                else:
                    return Container
            else:
                return ('array_data', 'data',)

    def __get_constructor(self, base, addl_fields):
        # TODO: fix this to be more maintainable and smarter
        existing_args = set()
        docval_args = list()
        new_args = list()
        if base is not None:
            for arg in get_docval(base.__init__):
                existing_args.add(arg['name'])
                if arg['name'] in addl_fields:
                    continue
                docval_args.append(arg)
        for f, field_spec in addl_fields.items():
            dtype = self.__get_type(field_spec)
            docval_arg = {'name': f, 'type': dtype, 'doc': field_spec.doc}
            if not field_spec.required:
                docval_arg['default'] = getattr(field_spec, 'default_value', None)
            docval_args.append(docval_arg)
            if f not in existing_args:
                new_args.append(f)
        # TODO: set __nwbfields__
        if base is None:
            @docval(*docval_args)
            def __init__(self, **kwargs):
                for f in new_args:
                    setattr(self, f, kwargs.get(f, None))
            return __init__
        else:
            @docval(*docval_args)
            def __init__(self, **kwargs):
                pargs, pkwargs = fmt_docval_args(base.__init__, kwargs)
                super(type(self), self).__init__(*pargs, **pkwargs)
                for f in new_args:
                    setattr(self, f, kwargs.get(f, None))
            return __init__

    @docval({"name": "namespace", "type": str, "doc": "the namespace containing the data_type"},
            {"name": "data_type", "type": str, "doc": "the data type to create a Container class for"},
            returns='the class for the given namespace and data_type', rtype=type)
    def get_container_cls(self, **kwargs):
        '''Get the container class from data type specification

        If no class has been associated with the ``data_type`` from ``namespace``,
        a class will be dynamically created and returned.
        '''
        namespace, data_type = getargs('namespace', 'data_type', kwargs)
        cls = self.__get_container_cls(namespace, data_type)
        if cls is None:
            spec = self.__ns_catalog.get_spec(namespace, data_type)
            dt_hier = self.__ns_catalog.get_hierarchy(namespace, data_type)
            parent_cls = None
            for t in dt_hier:
                parent_cls = self.__get_container_cls(namespace, t)
                if parent_cls is not None:
                    break
            bases = tuple()
            if parent_cls is not None:
                bases = (parent_cls,)
            else:
                if isinstance(spec, GroupSpec):
                    bases = (Container,)
                elif isinstance(spec, DatasetSpec):
                    bases = (Data,)
                else:
                    raise ValueError("Cannot generate class from %s" % type(spec))
                parent_cls = bases[0]
            name = data_type
            attr_names = self.__default_mapper_cls.get_attr_names(spec)
            fields = dict()
            for k, field_spec in attr_names.items():
                if not spec.is_inherited_spec(field_spec):
                    fields[k] = field_spec
            d = {'__init__': self.__get_constructor(parent_cls, fields)}
            cls = type(str(name), bases, d)
            self.register_container_type(namespace, data_type, cls)
        return cls

    def __get_container_cls(self, namespace, data_type):
        if namespace not in self.__container_types:
            return None
        if data_type not in self.__container_types[namespace]:
            return None
        ret = self.__container_types[namespace][data_type]
        if isinstance(ret, TypeSource):
            ret = self.__get_container_cls(ret.namespace, ret.data_type)
            if ret is not None:
                self.register_container_type(namespace, data_type, ret)
        return ret

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder, LinkBuilder),
             'doc': 'the builder to get the data_type for'})
    def get_builder_dt(self, **kwargs):
        '''
        Get the data_type of a builder
        '''
        builder = getargs('builder', kwargs)
        ret = builder.attributes.get(self.__ns_catalog.group_spec_cls.type_key())
        if ret is None:
            msg = "builder '%s' does not have a data_type" % builder.name
            raise ValueError(msg)

        if isinstance(ret, bytes):
            ret = ret.decode('UTF-8')

        return ret

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder, LinkBuilder),
             'doc': 'the builder to get the sub-specification for'})
    def get_builder_ns(self, **kwargs):
        '''
        Get the namespace of a builder
        '''
        builder = getargs('builder', kwargs)
        if isinstance(builder, LinkBuilder):
            builder = builder.builder
        ret = builder.attributes.get('namespace')
        if ret is None:
            msg = "builder '%s' does not have a namespace" % builder.name
            raise ValueError(msg)
        return ret

    @docval({'name': 'builder', 'type': Builder,
             'doc': 'the Builder object to get the corresponding Container class for'})
    def get_cls(self, **kwargs):
        ''' Get the class object for the given Builder '''
        builder = getargs('builder', kwargs)
        data_type = self.get_builder_dt(builder)
        namespace = self.get_builder_ns(builder)
        return self.get_container_cls(namespace, data_type)

    @docval({'name': 'spec', 'type': (DatasetSpec, GroupSpec), 'doc': 'the parent spec to search'},
            {'name': 'builder', 'type': (DatasetBuilder, GroupBuilder, LinkBuilder),
             'doc': 'the builder to get the sub-specification for'})
    def get_subspec(self, **kwargs):
        '''
        Get the specification from this spec that corresponds to the given builder
        '''
        spec, builder = getargs('spec', 'builder', kwargs)
        if isinstance(builder, LinkBuilder):
            builder_type = type(builder.builder)
        else:
            builder_type = type(builder)
        if issubclass(builder_type, DatasetBuilder):
            subspec = spec.get_dataset(builder.name)
        else:
            subspec = spec.get_group(builder.name)
        if subspec is None:
            # builder was generated from something with a data_type and a wildcard name
            if isinstance(builder, LinkBuilder):
                dt = self.get_builder_dt(builder.builder)
            else:
                dt = self.get_builder_dt(builder)
            if dt is not None:
                # TODO: this returns None when using subclasses
                ns = self.get_builder_ns(builder)
                hierarchy = self.__ns_catalog.get_hierarchy(ns, dt)
                for t in hierarchy:
                    subspec = spec.get_data_type(t)
                    if subspec is not None:
                        break
        return subspec

    def get_container_ns_dt(self, obj):
        container_cls = obj.__class__
        namespace, data_type = self.get_container_cls_dt(container_cls)
        return namespace, data_type

    def get_container_cls_dt(self, cls):
        return self.__data_types.get(cls, (None, None))

    @docval({'name': 'namespace', 'type': str,
             'doc': 'the namespace to get the container classes for', 'default': None})
    def get_container_classes(self, **kwargs):
        namespace = getargs('namespace', kwargs)
        ret = self.__data_types.keys()
        if namespace is not None:
            ret = filter(lambda x: self.__data_types[x][0] == namespace, ret)
        return list(ret)

    @docval({'name': 'obj', 'type': (Container, Builder), 'doc': 'the object to get the ObjectMapper for'},
            returns='the ObjectMapper to use for mapping the given object', rtype='ObjectMapper')
    def get_map(self, **kwargs):
        """ Return the ObjectMapper object that should be used for the given container """
        obj = getargs('obj', kwargs)
        # get the container class, and namespace/data_type
        if isinstance(obj, Container):
            container_cls = obj.__class__
            namespace, data_type = self.get_container_ns_dt(obj)
            if namespace is None:
                raise ValueError("class %s is not mapped to a data_type" % container_cls)
        else:
            data_type = self.get_builder_dt(obj)
            namespace = self.get_builder_ns(obj)
            container_cls = self.get_cls(obj)
        # now build the ObjectMapper class
        spec = self.__ns_catalog.get_spec(namespace, data_type)
        mapper = self.__mappers.get(container_cls)
        if mapper is None:
            mapper_cls = self.__default_mapper_cls
            for cls in container_cls.__mro__:
                tmp_mapper_cls = self.__mapper_cls.get(cls)
                if tmp_mapper_cls is not None:
                    mapper_cls = tmp_mapper_cls
                    break

            mapper = mapper_cls(spec)
            self.__mappers[container_cls] = mapper
        return mapper

    @docval({"name": "namespace", "type": str, "doc": "the namespace containing the data_type to map the class to"},
            {"name": "data_type", "type": str, "doc": "the data_type to map the class to"},
            {"name": "container_cls", "type": (TypeSource, type), "doc": "the class to map to the specified data_type"})
    def register_container_type(self, **kwargs):
        ''' Map a container class to a data_type '''
        namespace, data_type, container_cls = getargs('namespace', 'data_type', 'container_cls', kwargs)
        spec = self.__ns_catalog.get_spec(namespace, data_type)    # make sure the spec exists
        self.__container_types.setdefault(namespace, dict())
        self.__container_types[namespace][data_type] = container_cls
        self.__data_types.setdefault(container_cls, (namespace, data_type))
        setattr(container_cls, spec.type_key(), data_type)
        setattr(container_cls, 'namespace', namespace)

    @docval({"name": "container_cls", "type": type,
             "doc": "the Container class for which the given ObjectMapper class gets used for"},
            {"name": "mapper_cls", "type": type, "doc": "the ObjectMapper class to use to map"})
    def register_map(self, **kwargs):
        ''' Map a container class to an ObjectMapper class '''
        container_cls, mapper_cls = getargs('container_cls', 'mapper_cls', kwargs)
        if self.get_container_cls_dt(container_cls) == (None, None):
            raise ValueError('cannot register map for type %s - no data_type found' % container_cls)
        self.__mapper_cls[container_cls] = mapper_cls

    @docval({"name": "container", "type": Container, "doc": "the container to convert to a Builder"},
            {"name": "manager", "type": BuildManager,
             "doc": "the BuildManager to use for managing this build", 'default': None},
            {"name": "source", "type": str,
             "doc": "the source of container being built i.e. file path", 'default': None},
            {"name": "builder", "type": GroupBuilder, "doc": "the Builder to build on", 'default': None})
    def build(self, **kwargs):
        """ Build the GroupBuilder for the given Container"""
        container, manager, builder = getargs('container', 'manager', 'builder', kwargs)
        if manager is None:
            manager = BuildManager(self)
        attr_map = self.get_map(container)
        if attr_map is None:
            raise ValueError('No ObjectMapper found for container of type %s' % str(container.__class__.__name__))
        else:
            builder = attr_map.build(container, manager, builder=builder, source=getargs('source', kwargs))
        namespace, data_type = self.get_container_ns_dt(container)
        builder.set_attribute('namespace', namespace)
        builder.set_attribute(attr_map.spec.type_key(), data_type)
        return builder

    @docval({'name': 'builder', 'type': (DatasetBuilder, GroupBuilder),
             'doc': 'the builder to construct the Container from'},
            {'name': 'build_manager', 'type': BuildManager,
             'doc': 'the BuildManager for constructing', 'default': None})
    def construct(self, **kwargs):
        """ Construct the Container represented by the given builder """
        builder, build_manager = getargs('builder', 'build_manager', kwargs)
        if build_manager is None:
            build_manager = BuildManager(self)
        attr_map = self.get_map(builder)
        if attr_map is None:
            raise ValueError('No ObjectMapper found for builder of type %s'
                             % str(container.__class__.__name__))  # noqa: F821
        else:
            return attr_map.construct(builder, build_manager)

    @docval({"name": "container", "type": Container, "doc": "the container to convert to a Builder"},
            returns='The name a Builder should be given when building this container', rtype=str)
    def get_builder_name(self, **kwargs):
        ''' Get the name a Builder should be given '''
        container = getargs('container', kwargs)
        attr_map = self.get_map(container)
        if attr_map is None:
            raise ValueError('No ObjectMapper found for container of type %s' % str(container.__class__.__name__))
        else:
            return attr_map.get_builder_name(container)
