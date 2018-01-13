from src.model.model import *
from src.model.entity import *
import src.simulator.sourcehelper as SH
from src.model.helpers import get_assignment_targets, is_lambda

from functools import singledispatch, update_wrapper
import ast
import z3
import types
import operator
import logging
logger = logging.getLogger(__name__)

""" this code allows us to dispatch within a class """
def methoddispatch(func):
    dispatcher = singledispatch(func)
    def wrapper(*args, **kw):
        return dispatcher.dispatch(args[1].__class__)(*args, **kw)
    wrapper.register = dispatcher.register
    update_wrapper(wrapper, func)
    return wrapper



operator_to_operation = {
    ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul,
    ast.Mod: operator.mod,
    ast.Div: operator.truediv, ast.Pow: operator.pow, ast.BitXor: operator.xor,
    ast.BitAnd: operator.and_, ast.BitOr: operator.or_,
    ast.Lt: operator.lt, ast.Gt: operator.gt, ast.LtE: operator.le, ast.GtE: operator.ge,
    ast.Eq: operator.eq, ast.NotEq: operator.ne,
    ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.In: operator.contains, # ast.NotIn: need to find something for this one...
    # unary ops
    ast.Invert: operator.invert, ast.UAdd: operator.pos,
    ast.Not: operator.not_, ast.USub: operator.neg,
    # boolean ops, defined as lambdas because this way we have short circuiting, hopefully
    # ast.And: (lambda l, r: l and r), # unused, using z3.And and z3.Or now
    # ast.Or: (lambda l, r: l or r),  # unused, using z3.And and z3.Or now
    }

""" PORT TREATMENT """
def get_z3_value(port, name):
    z3_var = None
    if port.resource.domain == int:
        return z3.IntVal(port.value)
    elif port.resource.domain == float:
        return z3.RealVal(port.value)
    elif port.resource.domain == bool:
        return z3.BoolVal(port.value)
    elif type(port.resource.domain) is list:
        my_enum = z3.Datatype(name)
        for v in port.resource.domain:
            my_enum.declare(v)
        my_enum = my_enum.create()
        return getattr(my_enum, port.value)

def get_z3_variable(port, name):
    z3_var = None
    varname = "{}_{}".format(name,id(port))
    if port.resource.domain == int:
        return z3.Intvarname
    elif port.resource.domain == float:
        return z3.Real(varname)
    elif port.resource.domain == bool:
        return z3.Bool(varname)
    elif type(port.resource.domain) is list:
        enum = z3.Datatype(varname)
        for v in port.resource.domain:
            enum.declare(v)
        return enum

class Z3Converter(object):

    def __init__(self, z3_vars, entity, container):
        self.z3_vars = z3_vars
        self.entity = entity
        self.container = container
        self.target = None
        self.source = None

    def find_z3_variable(self, identifier):
        if isinstance(identifier, Port):
            path_to_port = get_path_to_attribute(self.entity, identifier)
            return self.z3_vars[identifier][path_to_port]
        else:
            return self.z3_vars[identifier+"_"+str(id(self.container))][identifier]

    @methoddispatch
    def to_z3(self, obj):
        print("\t\t","Nothing special for node of type", type(obj))
        return obj

    """ GENERAL TYPES """
    @to_z3.register(list)
    def _(self, obj):
        """ in a list, convert every one of the parts individually"""
        constraints = []
        for stmt in obj:
            new_constraint = self.to_z3(stmt)
            if type(new_constraint) == list:
                constraints.extend(new_constraint)
            else:
                constraints.append(new_constraint)
        return constraints

    @to_z3.register(str)
    def _(self, obj, z3_var_name_to_find=None):
        """
        This is a place where problems might/can/will occur.
        For now let's pretend we only call to_z3 on strings if they're variable names
        or port names in the form self.port.value
        """
        if not z3_var_name_to_find:
            z3_var_name_to_find = obj

        # early out for dt
        if z3_var_name_to_find == "dt":
            return self.z3_vars["dt"]

        referenced_port = None
        try:
            referenced_port = attrgetter(obj)(self.entity) # get the referenced port from the entity
            # line above is where the exception happens and we go into alternative path
            if z3_var_name_to_find not in self.z3_vars[referenced_port]:
                # if the value is not yet created, then create one!
                self.z3_vars[referenced_port][z3_var_name_to_find] = get_z3_variable(referenced_port, z3_var_name_to_find)
            return self.z3_vars[referenced_port][z3_var_name_to_find]

        except AttributeError:
            # we arrive here if it a python variable, not a port
            # a standard python variable, not a port, assume it's Real
            key = "{}_{}".format(obj, id(self.container))
            if key not in self.z3_vars:
                self.z3_vars[key] = {}

            if z3_var_name_to_find not in self.z3_vars[key]:
                self.z3_vars[key][z3_var_name_to_find] = z3.Real("{}_{}".format(z3_var_name_to_find, id(self.container)))

            return self.z3_vars[key][z3_var_name_to_find]
        # if obj not in self.z3_vars:
        #     self.z3_vars[obj] = (z3.Real(obj), None)
        #     # FIXME: not finished here!!!
        return self.z3_vars[obj][0]


    @to_z3.register(types.FunctionType)
    def _(self, obj):
        """This one is actually for normal Function objects, not for AST Nodes"""
        param_name = body_ast = None
        body_ast = SH.get_ast_body(obj)
        return self.to_z3(body_ast)

    """ AST TYPES """
    @to_z3.register(ast.NameConstant)
    def _(self, obj):
        return obj.value

    @to_z3.register(ast.Num)
    def _(self, obj):
        return obj.n

    @to_z3.register(ast.Str)
    def _(self, obj):
        return obj.s

    @to_z3.register(ast.Name)
    def _(self, obj):
        # if our parent is an Attribute, then we just return the string
        if not hasattr(obj, "parent"):
            # this happens if we only return the param value within the lambda
            return self.to_z3(obj.id)

        if isinstance(obj.parent, ast.Attribute):
            return obj.id
        # special treatment for dt, we dereference directly, no need for checking weird things
        elif obj.id == "dt":
            return self.to_z3(obj.id)
        #otherwise we dereference so we get the variable
        else:
            ancestor_assign = SH.get_ancestor_of_type(obj, (ast.Assign, ast.AugAssign))
            # are we part of an assignment?
            # yes (part of assignment):
            if ancestor_assign:
                in_value = SH.is_decendant_of(obj, ancestor_assign.value)
                # right:
                if in_value:
                    # there are no writes after this assignment, and this is also not an assignment to that variable
                    if count_following_assignments_with_name_on_left(obj.id, obj) == 0 and \
                        obj.id not in get_assignment_targets(ancestor_assign):
                        # don't add anything
                        return self.to_z3(obj.id)
                    # count occurrences on the left of assignments
                    # that's our variable name
                    new_varname = "{}_{}".format(obj.id, count_previous_assignments_with_name_on_left(obj.id, obj))
                    return self.to_z3(obj.id, new_varname)
                # left:
                else:
                    # if last assignment:
                    if count_following_assignments_with_name_on_left(obj.id, obj) == 0:
                        # don't add anything
                        return self.to_z3(obj.id)
                    else:
                        # increment count by one
                        # that's our variable name
                        new_varname = "{}_{}".format(obj.id, count_previous_assignments_with_name_on_left(obj.id, obj)+1)
                        return self.to_z3(obj.id, new_varname)

            # no (not part of assignment):
            # else:
            #     import pdb; pdb.set_trace()
            #     raise NotImplementedError("First time we come across a variable not part of an assignment")
            return self.to_z3(obj.id)

    @to_z3.register(ast.Attribute)
    def _(self, obj):
        # this means we first assemble the entire string (a.b.c)
        attr_name = "{}.{}".format(self.to_z3(obj.value), obj.attr)
        # if our parent is an Attribute, then we just return the string
        if isinstance(obj.parent, ast.Attribute):
            return attr_name
        #otherwise we dereference so we get the variable
        else:
            # assumption that the format is self.port.value or self.entity.port.value
            attr_name = ".".join(attr_name.split(".")[1:-1])

            ancestor_assign = SH.get_ancestor_of_type(obj, (ast.Assign, ast.AugAssign))
            # are we part of an assignment?
            # yes (part of assignment):
            if ancestor_assign:
                in_value = SH.is_decendant_of(obj, ancestor_assign.value)
                # right:
                if in_value:
                    if count_following_assignments_with_name_on_left(attr_name, obj) == 0 and \
                        attr_name not in get_assignment_targets(ancestor_assign):
                        # don't add anything
                        return self.to_z3(attr_name)
                    else:
                        # count occurrences on the left of assignments
                        # that's our port name
                        new_varname = "{}_{}".format(attr_name, count_previous_assignments_with_name_on_left(attr_name, obj))
                        return self.to_z3(attr_name, new_varname)
                # left:
                else:
                    # if last assignment:
                    if count_following_assignments_with_name_on_left(attr_name, obj) == 0:
                        # don't add anything
                        return self.to_z3(attr_name)
                    else:
                        # increment count by one
                        # that's our port name
                        new_varname = "{}_{}".format(attr_name, count_previous_assignments_with_name_on_left(attr_name, obj)+1)
                        return self.to_z3(attr_name, new_varname)
            # no (not part of assignment):
            # else:
                # import pdb; pdb.set_trace()
                # raise NotImplementedError("First time we come across a variable not part of an assignment")
            return self.to_z3(attr_name)

    @to_z3.register(ast.Assign)
    def _(self, obj):
        z3_constraints = []
        value = self.to_z3(obj.value)
        for target in obj.targets:
            assignee = self.to_z3(target)
            z3_constraints.append(assignee == value)
        return z3_constraints

    @to_z3.register(ast.AugAssign)
    def _(self, obj):
        """We manually need to look up the value and add it as a constant into the equation"""
        value = self.to_z3(obj.value)
        assignee = self.to_z3(obj.target)

        targetname = get_assignment_targets(obj)[0]
        targetname = ".".join(targetname.split(".")[1:-1])
        varname_for_value = "{}_{}".format(targetname, count_previous_assignments_with_name_on_left(targetname, obj.target))
        target_in_value = self.to_z3(targetname, varname_for_value)

        operation = operator_to_operation[type(obj.op)]
        return assignee == operation(target_in_value, value)

    @to_z3.register(ast.UnaryOp)
    def _(self, obj):
        operand = self.to_z3(obj.operand)
        operation = operator_to_operation[type(obj.op)]
        return operation(operand)

    @to_z3.register(ast.BinOp)
    def _(self, obj):
        right = self.to_z3(obj.right)
        left = self.to_z3(obj.left)
        operation = operator_to_operation[type(obj.op)]
        return operation(left, right)

    @to_z3.register(ast.BoolOp)
    def _(self, obj):
        vals = [self.to_z3(v) for v in obj.values]
        if type(obj.op) == ast.And:
            return z3.And(*vals)
        elif type(obj.op) == ast.Or:
            return z3.Or(*vals)
        else:
            raise Exception("We shouldn't be here!!!!")

    @to_z3.register(ast.Compare)
    def _(self, obj):
        operands = [self.to_z3(obj.left)]
        operands.extend( [self.to_z3(comp) for comp in obj.comparators])
        operators = [operator_to_operation[type(op)] for op in obj.ops]

        comparisons = zip(operators, operands, operands[1:])
        return z3.And([comp[0](comp[1], comp[2]) for comp in comparisons])

    @to_z3.register(ast.Return)
    def _(self, obj):
        value = self.to_z3(obj.value)
        ret_val = self.find_z3_variable(self.target) == value
        return ret_val

""" End of Class - Start of helpers """

def get_identifier_from_target(target):
    """Returns the name (variable) or the name.name.name (attribute/port) from a target"""

    if isinstance(target, ast.Name):
        return target.id
    elif isinstance(target, ast.Attribute):
        return "{}.{}".format(get_identifier_from_target(target.value), target.attr)
    elif type(target) == str:
        return target

    raise Exception("Don't know how we got here... something's off")


def count_previous_assignments_with_name_on_left(name, obj):
    ancestor_assign = SH.get_ancestor_of_type(obj, (ast.Assign, ast.AugAssign))
    previous_siblings = SH.get_all_previous_siblings(ancestor_assign)
    matching_assignments = extract_assignments_with_name_on_left(name, previous_siblings)
    # following_assignments = list(filter((lambda x: isinstance(x, (ast.Assign, ast.AugAssign))), following_siblings))
    # following_assignments_variable_targets = [t for fa in following_assignments for t in SH.get_targets_from_assignment(fa) if isinstance(t, ast.Name)]
    # following_with_matching_name = [var_target for var_target in following_assignments_variable_targets if var_target.id == obj.id]
    # following_assigns_to_var_count = len(following_with_matching_name)
    return len(matching_assignments)
    #
    # previous_assignments = list(filter((lambda x: isinstance(x, (ast.Assign, ast.AugAssign))), previous_siblings))
    # previous_assignments_variable_targets = [t for pa in previous_assignments for t in SH.get_targets_from_assignment(pa) if isinstance(t, ast.Name)]
    # previous_with_matching_name = [var_target for var_target in previous_assignments_variable_targets if var_target.id == obj.id]
    # previous_assigns_to_var_count = len(previous_with_matching_name)
    # return previous_assigns_to_var_count

def count_following_assignments_with_name_on_left(name, obj):
    ancestor_assign = SH.get_ancestor_of_type(obj, (ast.Assign, ast.AugAssign))
    following_siblings = SH.get_all_following_siblings(ancestor_assign)
    matching_assignments = extract_assignments_with_name_on_left(name, following_siblings)
    # following_assignments = list(filter((lambda x: isinstance(x, (ast.Assign, ast.AugAssign))), following_siblings))
    # following_assignments_variable_targets = [t for fa in following_assignments for t in SH.get_targets_from_assignment(fa) if isinstance(t, ast.Name)]
    # following_with_matching_name = [var_target for var_target in following_assignments_variable_targets if var_target.id == obj.id]
    # following_assigns_to_var_count = len(following_with_matching_name)
    return len(matching_assignments)

def extract_assignments_with_name_on_left(name, siblings):
    assignments = list(filter((lambda x: isinstance(x, (ast.Assign, ast.AugAssign))), siblings))
    assignments_variable_targets = [t for fa in assignments for t in SH.get_targets_from_assignment(fa) if isinstance(t, (ast.Name, ast.Attribute))]
    with_matching_name = [var_target for var_target in assignments_variable_targets if get_identifier_from_target(var_target) == name]
    return with_matching_name