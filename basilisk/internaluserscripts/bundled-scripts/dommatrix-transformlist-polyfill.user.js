// This Source Code Form is subject to the terms of the Mozilla Public
// License, v. 2.0. If a copy of the MPL was not distributed with this
// file, You can obtain one at http://mozilla.org/MPL/2.0/.
// ==UserScript==
// @name         DOMMatrix transform list constructor polyfill
// @namespace    internal-userscripts
// @description  Extends the DOMMatrix/DOMMatrixReadOnly string constructors to accept full CSS transform lists ("none", 3D functions, and units such as deg/px) when the native parser only understands unitless SVG 1.1 syntax.
// @match        *://*/*
// @grant        none
// @run-at       document-start
// ==/UserScript==
(function () {
  "use strict";

  var global = typeof window !== "undefined" ? window : null;
  if (!global) {
    return;
  }

  var NATIVE_PROBES = ["none", "rotate(45deg)", "translate3d(1px, 2px, 3px)"];
  var ANGLE_UNITS = { "deg": Math.PI / 180, "rad": 1, "grad": Math.PI / 200, "turn": 2 * Math.PI };
  var NUMBER_RE = /^([+-]?(?:\d+\.?\d*|\.\d+)(?:e[+-]?\d+)?)([a-z%]*)$/i;

  function identity() {
    return [1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1];
  }

  function multiply(a, b) {
    var result = new Array(16);
    for (var row = 0; row < 4; row++) {
      for (var col = 0; col < 4; col++) {
        var sum = 0;
        for (var k = 0; k < 4; k++) {
          sum += a[row * 4 + k] * b[k * 4 + col];
        }
        result[row * 4 + col] = sum;
      }
    }
    return result;
  }

  // Parses a CSS transform list into a DOMMatrix sequence init: 6 numbers for
  // a 2D list, 16 for a 3D list, or null when the string should be left to the
  // native constructor (including to reproduce its SyntaxError for genuinely
  // invalid input such as percentages or calc()).
  function compileTransformList(str) {
    if (typeof str !== "string") {
      return null;
    }
    var trimmed = str.trim();
    if (trimmed === "") {
      return null;
    }
    if (trimmed.toLowerCase() === "none") {
      return [1, 0, 0, 1, 0, 0];
    }

    var result = identity();
    var is3D = false;
    var rest = trimmed;
    var fnRE = /^([a-zA-Z0-9]+)\s*\(([^()]*)\)\s*,?\s*/;

    while (rest !== "") {
      var match = fnRE.exec(rest);
      if (!match) {
        return null;
      }
      var parsed = parseFunction(match[1].toLowerCase(), match[2]);
      if (!parsed) {
        return null;
      }
      // A transform list composes left to right: "A B" is the matrix A * B.
      result = multiply(parsed.matrix, result);
      is3D = is3D || parsed.is3D;
      rest = rest.slice(match[0].length);
    }

    return is3D ? result : [result[0], result[1], result[4], result[5], result[12], result[13]];
  }

  function parseFunction(name, argsStr) {
    var trimmed = argsStr.trim();
    var tokens = trimmed === "" ? [] : trimmed.split(/[\s,]+/);
    var values = [];
    var units = [];
    for (var i = 0; i < tokens.length; i++) {
      var m = NUMBER_RE.exec(tokens[i]);
      if (!m) {
        return null;
      }
      values.push(parseFloat(m[1]));
      units.push(m[2].toLowerCase());
    }

    // px lengths and unitless zero resolve at parse time; anything else does not.
    function lengthAt(i) {
      if (units[i] === "px") {
        return values[i];
      }
      if (units[i] === "" && values[i] === 0) {
        return 0;
      }
      return null;
    }
    function numberAt(i) {
      return units[i] === "" ? values[i] : null;
    }
    function angleAt(i) {
      if (Object.prototype.hasOwnProperty.call(ANGLE_UNITS, units[i])) {
        return values[i] * ANGLE_UNITS[units[i]];
      }
      return null;
    }
    function expectLengths(count) {
      var out = [];
      for (var i = 0; i < count; i++) {
        var v = lengthAt(i);
        if (v === null) {
          return null;
        }
        out.push(v);
      }
      return out;
    }

    var matrix;
    var v0, v1, v2, angle;

    if (name === "matrix") {
      if (tokens.length !== 6) {
        return null;
      }
      var nums = [];
      for (i = 0; i < 6; i++) {
        var n = numberAt(i);
        if (n === null) {
          return null;
        }
        nums.push(n);
      }
      matrix = identity();
      matrix[0] = nums[0];
      matrix[1] = nums[1];
      matrix[4] = nums[2];
      matrix[5] = nums[3];
      matrix[12] = nums[4];
      matrix[13] = nums[5];
      return { matrix: matrix, is3D: false };
    }

    if (name === "matrix3d") {
      if (tokens.length !== 16) {
        return null;
      }
      var items = [];
      for (i = 0; i < 16; i++) {
        var item = numberAt(i);
        if (item === null) {
          return null;
        }
        items.push(item);
      }
      return { matrix: items, is3D: true };
    }

    if (name === "translate") {
      if (tokens.length !== 1 && tokens.length !== 2) {
        return null;
      }
      var t = expectLengths(tokens.length);
      if (!t) {
        return null;
      }
      matrix = identity();
      matrix[12] = t[0];
      matrix[13] = tokens.length === 2 ? t[1] : 0;
      return { matrix: matrix, is3D: false };
    }

    if (name === "translatex" || name === "translatey" || name === "translatez") {
      if (tokens.length !== 1) {
        return null;
      }
      var tv = lengthAt(0);
      if (tv === null) {
        return null;
      }
      matrix = identity();
      if (name === "translatex") {
        matrix[12] = tv;
        return { matrix: matrix, is3D: false };
      }
      if (name === "translatey") {
        matrix[13] = tv;
        return { matrix: matrix, is3D: false };
      }
      matrix[14] = tv;
      return { matrix: matrix, is3D: true };
    }

    if (name === "translate3d") {
      if (tokens.length !== 3) {
        return null;
      }
      var t3 = expectLengths(3);
      if (!t3) {
        return null;
      }
      matrix = identity();
      matrix[12] = t3[0];
      matrix[13] = t3[1];
      matrix[14] = t3[2];
      return { matrix: matrix, is3D: true };
    }

    if (name === "scale") {
      if (tokens.length !== 1 && tokens.length !== 2) {
        return null;
      }
      v0 = numberAt(0);
      v1 = tokens.length === 2 ? numberAt(1) : v0;
      if (v0 === null || v1 === null) {
        return null;
      }
      matrix = identity();
      matrix[0] = v0;
      matrix[5] = v1;
      return { matrix: matrix, is3D: false };
    }

    if (name === "scalex" || name === "scaley" || name === "scalez") {
      if (tokens.length !== 1) {
        return null;
      }
      v0 = numberAt(0);
      if (v0 === null) {
        return null;
      }
      matrix = identity();
      if (name === "scalex") {
        matrix[0] = v0;
        return { matrix: matrix, is3D: false };
      }
      if (name === "scaley") {
        matrix[5] = v0;
        return { matrix: matrix, is3D: false };
      }
      matrix[10] = v0;
      return { matrix: matrix, is3D: true };
    }

    if (name === "scale3d") {
      if (tokens.length !== 3) {
        return null;
      }
      var s = [];
      for (i = 0; i < 3; i++) {
        s.push(numberAt(i));
      }
      if (s[0] === null || s[1] === null || s[2] === null) {
        return null;
      }
      matrix = identity();
      matrix[0] = s[0];
      matrix[5] = s[1];
      matrix[10] = s[2];
      return { matrix: matrix, is3D: true };
    }

    if (name === "rotate") {
      if (tokens.length !== 1) {
        return null;
      }
      angle = angleAt(0);
      if (angle === null) {
        return null;
      }
      matrix = identity();
      matrix[0] = Math.cos(angle);
      matrix[1] = Math.sin(angle);
      matrix[4] = -Math.sin(angle);
      matrix[5] = Math.cos(angle);
      return { matrix: matrix, is3D: false };
    }

    // rotateZ is an alias of rotate() and keeps the result 2D.
    if (name === "rotatex" || name === "rotatey" || name === "rotatez") {
      if (tokens.length !== 1) {
        return null;
      }
      angle = angleAt(0);
      if (angle === null) {
        return null;
      }
      if (name === "rotatez") {
        matrix = identity();
        matrix[0] = Math.cos(angle);
        matrix[1] = Math.sin(angle);
        matrix[4] = -Math.sin(angle);
        matrix[5] = Math.cos(angle);
        return { matrix: matrix, is3D: false };
      }
      return { matrix: axisRotation(name === "rotatex" ? [1, 0, 0] : [0, 1, 0], angle), is3D: true };
    }

    if (name === "rotate3d") {
      if (tokens.length !== 4) {
        return null;
      }
      v0 = numberAt(0);
      v1 = numberAt(1);
      v2 = numberAt(2);
      angle = angleAt(3);
      if (v0 === null || v1 === null || v2 === null || angle === null) {
        return null;
      }
      return { matrix: axisRotation([v0, v1, v2], angle), is3D: true };
    }

    if (name === "skewx" || name === "skewy") {
      if (tokens.length !== 1) {
        return null;
      }
      angle = angleAt(0);
      if (angle === null) {
        return null;
      }
      matrix = identity();
      if (name === "skewx") {
        matrix[4] = Math.tan(angle);
      } else {
        matrix[1] = Math.tan(angle);
      }
      return { matrix: matrix, is3D: false };
    }

    if (name === "perspective") {
      if (tokens.length !== 1) {
        return null;
      }
      var d = lengthAt(0);
      if (d === null || d < 0) {
        return null;
      }
      // Mainstream engines clamp the divisor to a minimum of 1.
      matrix = identity();
      matrix[11] = -1 / Math.max(d, 1);
      return { matrix: matrix, is3D: true };
    }

    return null;
  }

  function axisRotation(axis, angle) {
    var x = axis[0], y = axis[1], z = axis[2];
    var length = Math.sqrt(x * x + y * y + z * z);
    if (length === 0) {
      return identity();
    }
    x /= length;
    y /= length;
    z /= length;
    var c = Math.cos(angle);
    var s = Math.sin(angle);
    var t = 1 - c;
    return [
      t * x * x + c,     t * x * y + s * z, t * x * z - s * y, 0,
      t * x * y - s * z, t * y * y + c,     t * y * z + s * x, 0,
      t * x * z + s * y, t * y * z - s * x, t * z * z + c,     0,
      0,                 0,                 0,                 1
    ];
  }

  // Wraps a native DOMMatrix-style constructor so that string init values the
  // native parser rejects are re-parsed here. Everything the native parser
  // already accepts keeps its exact native behavior and error messages.
  function wrapConstructor(nativeCtor) {
    if (typeof nativeCtor !== "function") {
      return null;
    }

    var Shim = function (init) {
      var args = arguments;
      if (new.target === undefined) {
        // Preserve the native call-as-function behavior (a TypeError).
        return nativeCtor.apply(this, args);
      }
      try {
        return Reflect.construct(nativeCtor, args, new.target);
      } catch (ex) {
        if (args.length === 1 && typeof init === "string") {
          var compiled = compileTransformList(init);
          if (compiled) {
            return Reflect.construct(nativeCtor, [compiled], new.target);
          }
        }
        throw ex;
      }
    };

    try {
      Shim.prototype = nativeCtor.prototype;
    } catch (e) {}

    var statics = ["fromMatrix", "fromFloat32Array", "fromFloat64Array"];
    for (var i = 0; i < statics.length; i++) {
      (function (key) {
        if (typeof nativeCtor[key] === "function") {
          Shim[key] = function () {
            return nativeCtor[key].apply(nativeCtor, arguments);
          };
        }
      })(statics[i]);
    }

    try {
      Object.defineProperty(Shim, "name", { value: nativeCtor.name, configurable: true });
    } catch (e) {}

    return Shim;
  }

  var needsPatch = false;
  try {
    for (var i = 0; i < NATIVE_PROBES.length; i++) {
      new global.DOMMatrix(NATIVE_PROBES[i]);
    }
  } catch (e) {
    needsPatch = true;
  }

  if (needsPatch) {
    var wrappedDOMMatrix = wrapConstructor(global.DOMMatrix);
    var wrappedDOMMatrixReadOnly = wrapConstructor(global.DOMMatrixReadOnly);
    var patched = false;
    if (wrappedDOMMatrix) {
      global.DOMMatrix = wrappedDOMMatrix;
      patched = true;
    }
    if (wrappedDOMMatrixReadOnly) {
      global.DOMMatrixReadOnly = wrappedDOMMatrixReadOnly;
      patched = true;
    }
    if (patched) {
      try {
        global.__internalUserscriptsDOMMatrixTransformListPolyfill = true;
      } catch (e) {}
    }
  }
})();
