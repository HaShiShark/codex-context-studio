import { parse } from 'acorn';

import type { ToolEvent } from './types';

type AstNode = {
  type: string;
  start?: number;
  end?: number;
  [key: string]: unknown;
};

type StaticValue = null | boolean | number | string | StaticValue[] | { [key: string]: StaticValue };
type StaticEnvironment = Map<string, StaticValue>;

export interface ExecNestedToolCall {
  name: string;
  arguments: StaticValue | string;
}

export interface ExecDisplayAnalysis {
  calls: ExecNestedToolCall[];
  exact: boolean;
}

const UNRESOLVED = Symbol('unresolved');
const COMMAND_TOOL_NAMES = new Set(['shell_command', 'exec_command', 'write_stdin']);

function isNode(value: unknown): value is AstNode {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value) && typeof (value as AstNode).type === 'string');
}

function isStaticObject(value: StaticValue): value is { [key: string]: StaticValue } {
  return Boolean(value && typeof value === 'object' && !Array.isArray(value));
}

function propertyName(node: AstNode, environment: StaticEnvironment): string | null {
  if (!node.computed && isNode(node.property) && node.property.type === 'Identifier') {
    return String(node.property.name || '');
  }
  if (!isNode(node.property)) {
    return null;
  }
  const value = evaluateStatic(node.property, environment);
  return typeof value === 'string' || typeof value === 'number' ? String(value) : null;
}

function evaluateTemplateLiteral(node: AstNode, environment: StaticEnvironment): StaticValue | typeof UNRESOLVED {
  const quasis = Array.isArray(node.quasis) ? node.quasis : [];
  const expressions = Array.isArray(node.expressions) ? node.expressions : [];
  let result = '';

  for (let index = 0; index < quasis.length; index += 1) {
    const quasi = quasis[index];
    if (!isNode(quasi) || !quasi.value || typeof quasi.value !== 'object') {
      return UNRESOLVED;
    }
    const cooked = (quasi.value as Record<string, unknown>).cooked;
    const raw = (quasi.value as Record<string, unknown>).raw;
    result += typeof cooked === 'string' ? cooked : typeof raw === 'string' ? raw : '';

    if (index < expressions.length) {
      const expression = expressions[index];
      if (!isNode(expression)) {
        return UNRESOLVED;
      }
      const value = evaluateStatic(expression, environment);
      if (value === UNRESOLVED || (typeof value === 'object' && value !== null)) {
        return UNRESOLVED;
      }
      result += String(value);
    }
  }

  return result;
}

function evaluateStatic(node: AstNode, environment: StaticEnvironment): StaticValue | typeof UNRESOLVED {
  if (node.type === 'Literal') {
    const value = node.value;
    return value === null || ['string', 'number', 'boolean'].includes(typeof value)
      ? value as StaticValue
      : UNRESOLVED;
  }

  if (node.type === 'Identifier') {
    const name = String(node.name || '');
    return environment.has(name) ? environment.get(name) as StaticValue : UNRESOLVED;
  }

  if (node.type === 'TemplateLiteral') {
    return evaluateTemplateLiteral(node, environment);
  }

  if (node.type === 'ArrayExpression') {
    const result: StaticValue[] = [];
    for (const element of Array.isArray(node.elements) ? node.elements : []) {
      if (!isNode(element)) {
        return UNRESOLVED;
      }
      if (element.type === 'SpreadElement' && isNode(element.argument)) {
        const spread = evaluateStatic(element.argument, environment);
        if (!Array.isArray(spread)) {
          return UNRESOLVED;
        }
        result.push(...spread);
        continue;
      }
      const value = evaluateStatic(element, environment);
      if (value === UNRESOLVED) {
        return UNRESOLVED;
      }
      result.push(value);
    }
    return result;
  }

  if (node.type === 'ObjectExpression') {
    const result: Record<string, StaticValue> = {};
    for (const property of Array.isArray(node.properties) ? node.properties : []) {
      if (!isNode(property)) {
        return UNRESOLVED;
      }
      if (property.type === 'SpreadElement' && isNode(property.argument)) {
        const spread = evaluateStatic(property.argument, environment);
        if (!isStaticObject(spread as StaticValue)) {
          return UNRESOLVED;
        }
        Object.assign(result, spread);
        continue;
      }
      if (property.type !== 'Property' || !isNode(property.value)) {
        return UNRESOLVED;
      }
      const key = property.computed
        ? isNode(property.key)
          ? evaluateStatic(property.key, environment)
          : UNRESOLVED
        : isNode(property.key)
          ? String(property.key.name ?? property.key.value ?? '')
          : UNRESOLVED;
      const value = evaluateStatic(property.value, environment);
      if ((typeof key !== 'string' && typeof key !== 'number') || value === UNRESOLVED) {
        return UNRESOLVED;
      }
      result[String(key)] = value;
    }
    return result;
  }

  if (node.type === 'MemberExpression' && isNode(node.object)) {
    const object = evaluateStatic(node.object, environment);
    const key = propertyName(node, environment);
    if (object === UNRESOLVED || key === null) {
      return UNRESOLVED;
    }
    if (Array.isArray(object)) {
      const index = Number(key);
      return Number.isInteger(index) && index >= 0 && index < object.length ? object[index] : UNRESOLVED;
    }
    return isStaticObject(object) && key in object ? object[key] : UNRESOLVED;
  }

  if (node.type === 'BinaryExpression' && node.operator === '+' && isNode(node.left) && isNode(node.right)) {
    const left = evaluateStatic(node.left, environment);
    const right = evaluateStatic(node.right, environment);
    if (left === UNRESOLVED || right === UNRESOLVED || typeof left === 'object' || typeof right === 'object') {
      return UNRESOLVED;
    }
    return typeof left === 'string' || typeof right === 'string' ? `${left}${right}` : Number(left) + Number(right);
  }

  if (node.type === 'UnaryExpression' && isNode(node.argument)) {
    const value = evaluateStatic(node.argument, environment);
    if (value === UNRESOLVED || typeof value === 'object') {
      return UNRESOLVED;
    }
    if (node.operator === '!') return !value;
    if (node.operator === '-' && typeof value === 'number') return -value;
    if (node.operator === '+' && typeof value === 'number') return value;
  }

  if (node.type === 'ConditionalExpression' && isNode(node.test)) {
    const test = evaluateStatic(node.test, environment);
    const branch = test === true ? node.consequent : test === false ? node.alternate : null;
    return isNode(branch) ? evaluateStatic(branch, environment) : UNRESOLVED;
  }

  return UNRESOLVED;
}

function bindPattern(pattern: AstNode, value: StaticValue, environment: StaticEnvironment): boolean {
  if (pattern.type === 'Identifier') {
    environment.set(String(pattern.name || ''), value);
    return true;
  }

  if (pattern.type === 'ArrayPattern' && Array.isArray(value)) {
    const elements = Array.isArray(pattern.elements) ? pattern.elements : [];
    return elements.every((element, index) => !isNode(element) || bindPattern(element, value[index] ?? null, environment));
  }

  if (pattern.type === 'ObjectPattern' && isStaticObject(value)) {
    return (Array.isArray(pattern.properties) ? pattern.properties : []).every((property) => {
      if (!isNode(property) || property.type !== 'Property' || !isNode(property.key) || !isNode(property.value)) {
        return false;
      }
      const key = String(property.key.name ?? property.key.value ?? '');
      return key in value && bindPattern(property.value, value[key], environment);
    });
  }

  return false;
}

function sourceSlice(node: AstNode, source: string): string {
  return typeof node.start === 'number' && typeof node.end === 'number'
    ? source.slice(node.start, node.end)
    : '';
}

function nestedToolName(node: AstNode, environment: StaticEnvironment): string | null {
  if (node.type !== 'CallExpression' || !isNode(node.callee) || node.callee.type !== 'MemberExpression') {
    return null;
  }
  if (!isNode(node.callee.object) || node.callee.object.type !== 'Identifier' || node.callee.object.name !== 'tools') {
    return null;
  }
  return propertyName(node.callee, environment);
}

function mapCallParts(node: AstNode, environment: StaticEnvironment) {
  if (node.type !== 'CallExpression' || !isNode(node.callee) || node.callee.type !== 'MemberExpression') {
    return null;
  }
  if (propertyName(node.callee, environment) !== 'map' || !isNode(node.callee.object)) {
    return null;
  }
  const values = evaluateStatic(node.callee.object, environment);
  const callback = Array.isArray(node.arguments) ? node.arguments[0] : null;
  if (!Array.isArray(values) || !isNode(callback) || !['ArrowFunctionExpression', 'FunctionExpression'].includes(callback.type)) {
    return null;
  }
  return { values, callback };
}

interface WalkState {
  calls: ExecNestedToolCall[];
  exact: boolean;
  source: string;
}

function walkChildren(node: AstNode, environment: StaticEnvironment, deterministic: boolean, state: WalkState) {
  Object.entries(node).forEach(([key, value]) => {
    if (['type', 'start', 'end', 'loc', 'range'].includes(key)) return;
    if (isNode(value)) {
      walkNode(value, environment, deterministic, state);
    } else if (Array.isArray(value)) {
      value.forEach((item) => {
        if (isNode(item)) walkNode(item, environment, deterministic, state);
      });
    }
  });
}

function walkNode(node: AstNode, environment: StaticEnvironment, deterministic: boolean, state: WalkState): void {
  if (node.type === 'Program' || node.type === 'BlockStatement') {
    const blockEnvironment = node.type === 'Program' ? environment : new Map(environment);
    (Array.isArray(node.body) ? node.body : []).forEach((statement) => {
      if (isNode(statement)) walkNode(statement, blockEnvironment, deterministic, state);
    });
    return;
  }

  if (node.type === 'VariableDeclaration') {
    (Array.isArray(node.declarations) ? node.declarations : []).forEach((declaration) => {
      if (!isNode(declaration) || !isNode(declaration.id)) return;
      if (isNode(declaration.init)) {
        walkNode(declaration.init, environment, deterministic, state);
        const value = evaluateStatic(declaration.init, environment);
        if (value !== UNRESOLVED) bindPattern(declaration.id, value, environment);
      }
    });
    return;
  }

  if (node.type === 'AssignmentExpression' && isNode(node.right)) {
    walkNode(node.right, environment, deterministic, state);
    if (isNode(node.left) && node.left.type === 'Identifier') {
      const value = evaluateStatic(node.right, environment);
      if (value !== UNRESOLVED) environment.set(String(node.left.name || ''), value);
    }
    return;
  }

  if (node.type === 'CallExpression') {
    const toolName = nestedToolName(node, environment);
    if (toolName) {
      if (!deterministic) state.exact = false;
      const argument = Array.isArray(node.arguments) && isNode(node.arguments[0]) ? node.arguments[0] : null;
      const value = argument ? evaluateStatic(argument, environment) : null;
      state.calls.push({
        name: toolName,
        arguments: value === UNRESOLVED && argument ? sourceSlice(argument, state.source) : value === UNRESOLVED ? '' : value,
      });
      return;
    }

    const map = mapCallParts(node, environment);
    if (map) {
      const parameters = Array.isArray(map.callback.params) ? map.callback.params : [];
      const body = isNode(map.callback.body) ? map.callback.body : null;
      if (!body || !isNode(parameters[0])) {
        state.exact = false;
        return;
      }
      map.values.forEach((value, index) => {
        const callbackEnvironment = new Map(environment);
        const bound = bindPattern(parameters[0] as AstNode, value, callbackEnvironment);
        if (isNode(parameters[1])) bindPattern(parameters[1], index, callbackEnvironment);
        if (isNode(parameters[2])) bindPattern(parameters[2], map.values, callbackEnvironment);
        if (!bound) state.exact = false;
        walkNode(body, callbackEnvironment, deterministic && bound, state);
      });
      return;
    }
  }

  if (node.type === 'ConditionalExpression' && isNode(node.test)) {
    walkNode(node.test, environment, deterministic, state);
    const test = evaluateStatic(node.test, environment);
    if (test === true && isNode(node.consequent)) {
      walkNode(node.consequent, new Map(environment), deterministic, state);
    } else if (test === false && isNode(node.alternate)) {
      walkNode(node.alternate, new Map(environment), deterministic, state);
    } else {
      if (isNode(node.consequent)) walkNode(node.consequent, new Map(environment), false, state);
      if (isNode(node.alternate)) walkNode(node.alternate, new Map(environment), false, state);
    }
    return;
  }

  if (node.type === 'LogicalExpression' && isNode(node.left)) {
    walkNode(node.left, environment, deterministic, state);
    const left = evaluateStatic(node.left, environment);
    const shouldWalkRight = node.operator === '&&'
      ? left === true
      : node.operator === '||'
        ? left === false
        : node.operator === '??'
          ? left === null
          : false;
    const shouldSkipRight = node.operator === '&&'
      ? left === false
      : node.operator === '||'
        ? left === true
        : node.operator === '??'
          ? left !== UNRESOLVED && left !== null
          : false;

    if (isNode(node.right) && !shouldSkipRight) {
      walkNode(node.right, new Map(environment), deterministic && shouldWalkRight, state);
    }
    return;
  }

  if (node.type === 'IfStatement') {
    const test = isNode(node.test) ? evaluateStatic(node.test, environment) : UNRESOLVED;
    if (test === true && isNode(node.consequent)) {
      walkNode(node.consequent, new Map(environment), deterministic, state);
    } else if (test === false && isNode(node.alternate)) {
      walkNode(node.alternate, new Map(environment), deterministic, state);
    } else {
      if (isNode(node.consequent)) walkNode(node.consequent, new Map(environment), false, state);
      if (isNode(node.alternate)) walkNode(node.alternate, new Map(environment), false, state);
    }
    return;
  }

  if (['ArrowFunctionExpression', 'FunctionExpression', 'FunctionDeclaration'].includes(node.type)) {
    return;
  }

  if (['ForStatement', 'ForInStatement', 'ForOfStatement', 'WhileStatement', 'DoWhileStatement', 'SwitchStatement', 'TryStatement'].includes(node.type)) {
    walkChildren(node, new Map(environment), false, state);
    return;
  }

  walkChildren(node, environment, deterministic, state);
}

export function analyzeExecSource(source: string): ExecDisplayAnalysis | null {
  const trimmed = source.trim();
  if (!trimmed) return null;

  try {
    const program = parse(trimmed, {
      ecmaVersion: 'latest',
      sourceType: 'module',
      allowAwaitOutsideFunction: true,
    }) as unknown as AstNode;
    const state: WalkState = { calls: [], exact: true, source: trimmed };
    walkNode(program, new Map(), true, state);
    return state.calls.length ? { calls: state.calls, exact: state.exact } : null;
  } catch {
    return null;
  }
}

function commandDetail(value: StaticValue | string): string {
  if (typeof value === 'string') return value;
  if (!isStaticObject(value)) return '';
  const command = value.command ?? value.cmd ?? value.input ?? value.stdin;
  if (Array.isArray(command)) return command.map(String).join(' ');
  return typeof command === 'string' || typeof command === 'number' ? String(command) : '';
}

export function projectExecToolEvent(event: ToolEvent): ToolEvent[] | null {
  if (event.name !== 'exec' || typeof event.arguments !== 'string') return null;
  const status = String(event.status || '').toLowerCase();
  if (status && status !== 'completed') return null;
  const analysis = analyzeExecSource(event.arguments);
  if (!analysis?.exact || !analysis.calls.length) return null;

  const singleCall = analysis.calls.length === 1;
  return analysis.calls.map((call, index) => ({
    name: call.name,
    arguments: call.arguments,
    call_id: `${event.call_id || 'exec'}:nested:${index}`,
    output_preview: singleCall ? event.output_preview : '',
    raw_output: singleCall ? event.raw_output : '',
    display_detail: commandDetail(call.arguments),
    display_result: singleCall ? event.display_result : '',
    status: event.status,
    metadata: {
      display_projection: 'exec_nested_call',
      parent_call_id: event.call_id || '',
      aggregate_output_only: !singleCall,
      command_tool: COMMAND_TOOL_NAMES.has(call.name),
    },
  }));
}
