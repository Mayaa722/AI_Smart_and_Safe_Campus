"""Conservative ASCII-math presentation. Never evaluate source/model text."""
import re

GREEK = {'rho', 'theta', 'phi', 'pi', 'alpha', 'beta', 'gamma', 'delta',
         'lambda', 'sigma', 'omega', 'epsilon', 'mu'}
FUNCTIONS = {'sin', 'cos', 'tan', 'sinh', 'cosh', 'tanh', 'coth', 'sech', 'csch', 'sqrt', 'exp', 'log', 'ln', 'abs'}
TOKEN = re.compile(r"\s*(\d+(?:\.\d+)?|[mcxy](?=[xy](?:\b|_))|[xy](?=(?:cos|sin)\()|[A-Za-z]+(?:_[A-Za-z0-9]+)?|\*\*|<=|>=|[+\-*/^=(),<>']|≤|≥)")
AMBIGUOUS_EXPONENTIAL=re.compile(r'\be\^\s*[+-]?\d+(?:\.\d+)?[A-Za-z]')


def symbol(name):
    if name in GREEK:
        return '\\' + name
    if name == 'sum_i':
        return r'\sum_i'
    if '_' in name:
        base, sub = name.split('_', 1)
        if sub == 'bar':
            return r'\bar{' + symbol(base) + '}'
        return symbol(base) + '_{' + (symbol(sub) if len(sub) == 1 or sub in GREEK else r'\mathrm{' + sub + '}') + '}'
    if re.fullmatch(r'[A-Za-z]', name) or name in {'dx', 'dy', 'dt', 'dE', 'dm', 'dc'}:
        return name
    # Common lecture notation uses juxtaposition for these products.
    if re.fullmatch(r'[mcxy][xy]', name):
        return ' '.join(name)
    raise ValueError('Not a supported math symbol')


class Parser:
    def __init__(self, text):
        self.tokens = []
        position = 0
        while position < len(text):
            match = TOKEN.match(text, position)
            if not match:
                break
            self.tokens.append((match.group(1), match.end()))
            position = match.end()
        self.i = 0
        self.operations = 0

    def peek(self):
        return self.tokens[self.i][0] if self.i < len(self.tokens) else ''

    def take(self):
        value = self.peek()
        self.i += 1
        return value

    def atom(self):
        value = self.take()
        if value == '(':
            parts = [self.relation()]
            while self.peek() == ',':
                self.take()
                parts.append(self.relation())
            if self.take() != ')':
                raise ValueError('Unclosed math parentheses')
            return r'\left(' + ', '.join(parts) + r'\right)'
        if value in FUNCTIONS:
            primes=''
            while self.peek()=="'":
                self.take();primes+=r'\prime';self.operations+=1
            function_power=None
            if self.peek() in {'^','**'}:
                self.take();function_power=self.power();self.operations+=1
            argument = self.atom()
            self.operations += 1
            if value == 'sqrt':
                if argument.startswith(r'\left('):
                    argument = argument[6:-7]
                return r'\sqrt{' + argument + '}'
            if value == 'abs':
                return r'\left|' + argument + r'\right|'
            operator=(r'\operatorname{'+value+'}') if value in {'sech','csch'} else '\\'+value
            if primes:operator+='^{'+primes+'}'
            if function_power is not None:operator='{'+operator+'}^{'+function_power+'}'
            return operator+' '+argument
        if value == 'sum_i':
            self.operations += 1
            return r'\sum_i ' + self.power()
        if re.fullmatch(r'\d+(?:\.\d+)?', value):
            return value
        # m x_i, rather than an unintended variable named mx_i.
        if re.fullmatch(r'mx_(?:i|bar)', value):
            return 'm ' + symbol(value[1:])
        return symbol(value)

    def power(self):
        if self.peek() in {'+', '-'}:
            return self.take() + self.power()
        left = self.atom()
        primes=''
        while self.peek()=="'":
            self.take();primes+=r'\prime';self.operations+=1
        if primes:left+='^{'+primes+'}'
        if self.peek() in {'^', '**'}:
            self.take()
            right = self.power()
            self.operations += 1
            if right.startswith(r'\left(') and right.endswith(r'\right)'):
                right = right[6:-7]
            left = '{' + left + '}^{' + right + '}'
        return left

    def product(self):
        left = self.power()
        while True:
            op = self.peek()
            if op in {'*', '/'}:
                self.take()
                right = self.power()
                self.operations += 1
                if op == '/':
                    left = r'\frac{' + left + '}{' + right + '}'
                else:
                    left += r' \cdot ' + right
            elif op and (op == '(' or op in FUNCTIONS or re.fullmatch(r'\d+(?:\.\d+)?', op) or self.is_symbol(op)):
                left += ' ' + self.power()
                self.operations += 1
            else:
                break
        return left

    @staticmethod
    def is_symbol(value):
        try:
            symbol(value)
            return True
        except ValueError:
            return False

    def expression(self):
        left = self.product()
        while self.peek() in {'+', '-'}:
            op = self.take()
            left += ' ' + op + ' ' + self.product()
            self.operations += 1
        return left

    def relation(self):
        left = self.expression()
        while self.peek() in {'=', '<', '>', '≤', '≥', '<=', '>='}:
            op = self.take()
            left += ' ' + {'≤': r'\le', '≥': r'\ge', '<=': r'\le', '>=': r'\ge'}.get(op, op) + ' ' + self.expression()
            self.operations += 1
        return left


def ascii_latex(expression):
    if AMBIGUOUS_EXPONENTIAL.search(expression):raise ValueError('Ambiguous exponential grouping; check the source rather than assuming e^(2*x).')
    expression = re.sub(r'\b([xy])(?=(?:cos|sin)\()', r'\1 ', expression)
    parser = Parser(expression.strip())
    result = parser.relation()
    if parser.i != len(parser.tokens) or not parser.tokens or parser.tokens[-1][1] != len(expression.strip()):
        raise ValueError('Only complete, supported math expressions can be converted')
    return result


# Match equation-like spans, not every occurrence of a letter in prose.
START = re.compile(r'(?<![\w\\])(?:'+ '|'.join(sorted(FUNCTIONS|GREEK|{'sum_i','dE','dy','dx'},key=len,reverse=True))+
                   r"|[A-Za-z](?:_(?:[A-Za-z0-9]+))?)(?=\s*(?:'{1,3}\s*)?(?:=|/|\^|\*|cos\(|sin\(|\())|(?<!\w)\d+(?=\s*/\s*(?:sqrt\(|\d)|\s*[≤≥<>])|(?<!\w)\((?=[xy]\s*[-+])")


def math_markdown(text):
    """Add KaTeX delimiters to recognized math; preserve existing math/code."""
    text = re.sub(r'\\\[([\s\S]*?)\\\]', lambda m: '$$' + m.group(1) + '$$', text)
    text = re.sub(r'\\\(([^\n]*?)\\\)', lambda m: '$' + m.group(1) + '$', text)
    protected = re.split(r'(\$\$[\s\S]*?\$\$|(?<!\\)\$[^$\n]+\$|```[\s\S]*?```|`[^`\n]+`|https?://\S+|\[[^\]]*\]\([^)]*\))', text)
    for part in range(0, len(protected), 2):
        plain = protected[part]
        out, cursor = [], 0
        for match in START.finditer(plain):
            if match.start() < cursor:
                continue
            candidate = plain[match.start():]
            if AMBIGUOUS_EXPONENTIAL.search(candidate.split('\n',1)[0]):continue
            # Stop at prose/markup; retain the longest fully parseable prefix.
            parser = Parser(candidate)
            best = None
            for length in range(1, min(len(parser.tokens), 100) + 1):
                end = parser.tokens[length - 1][1]
                segment = candidate[:end].rstrip()
                # Do not consume sentence words: only symbols/functions/numbers.
                try:
                    probe = Parser(segment)
                    latex = probe.relation()
                    if probe.i == len(probe.tokens) and probe.operations and len(segment) > 2:
                        best = (end, latex)
                except (ValueError, IndexError, RecursionError):
                    pass
                token = parser.tokens[length - 1][0]
                if re.fullmatch(r'[A-Za-z]+(?:_[A-Za-z0-9]+)?', token) and token not in FUNCTIONS and not Parser.is_symbol(token) and not re.fullmatch(r'mx_(?:i|bar)', token):
                    break
            if best:
                end, latex = best
                # Avoid turning an unclosed/cut-off formula into a misleading prefix.
                rest = candidate[end:].lstrip()
                if rest.startswith(('=', '+', '-', '*', '/', '^')):
                    continue
                out.append(plain[cursor:match.start()])
                out.append('$' + latex + '$')
                cursor = match.start() + end
        out.append(plain[cursor:])
        protected[part] = ''.join(out)
    return ''.join(protected)
