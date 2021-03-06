// Pass just "key" to receive a straight string back from the language file
// Pass "key" and "args" to receive a translated string with variables filled in
// Pass an element and the el will be filled with <span>s of each chunk of string
//    Variables will be given class "lang_[line]_[variablename]" when filling in variables
function _l(key, args, el, keep) {
	var parts = _tl_core(key, args);

	if (el && !keep) {
		while (el.hasChildNodes()) {
			el.removeChild(el.firstChild);
		}
	}

	var text = "";
	for (var i = 0; i < parts.length; i++) {
		text += parts[i].text;
		if (el && parts[i].arg_key) {
			createEl("span", { "textContent": parts[i].text, "class": "lang_" + key + "_" + parts[i].arg_key}, el);
		}
		else if (el) {
			createEl("span", { "textContent": parts[i].text }, el);
		}
	}
	return text;

}

function _tl_core(key, args) {
	// returns a list of { text, arg_key } objects for processing usage
	if (!(key in lang)) {
		return [ { "text": "[[ " + key + " ]]", "arg_key": null } ];
	}

	var line = lang[key];
	if (!args) return [ { "text": line, "arg_key": null } ];

	var parts = [];
	var var_found, suffix_found, plural_found, arg_key, start_of_plural, whole_plural;
	while (line.length > 0) {
		var_found = line.indexOf("%(");
		suffix_found = line.indexOf("#(");
		plural_found = line.indexOf("&(");

		// I feel so dirty but if we don't do this, the not-found strings will always come first
		var_found = var_found === -1 ? 100000 : var_found;
		suffix_found = suffix_found === -1 ? 100000 : suffix_found;
		plural_found = plural_found === -1 ? 100000 : plural_found;

		if ((var_found !== 100000) && (var_found < suffix_found) && (var_found < plural_found)) {
			var arg_key = line.substr(var_found + 2, line.indexOf(")", var_found) - (var_found + 2));
			if (arg_key in args) {
				parts.push({ "text": line.substr(0, var_found), "arg_key": null });
				parts.push({ "text": args[arg_key], "arg_key": arg_key });
				line = line.substr(line.indexOf(")", var_found) + 1);
			}
		}

		else if ((suffix_found !== 100000) && (suffix_found < var_found) && (suffix_found < plural_found)) {
			arg_key = line.substr(suffix_found + 2, line.indexOf(")", suffix_found) - (suffix_found + 2));
			if (arg_key in args) {
				parts.push({ "text": line.substr(0, suffix_found), "arg_key": null });
				parts.push({ "text": _get_suffixed_number(args[arg_key]), "arg_key": arg_key });
				line = line.substr(line.indexOf(")", suffix_found) + 1);
			}
		}

		else if ((plural_found !== 100000) && (plural_found < var_found) && (plural_found < suffix_found)) {
			arg_key = line.substr(plural_found + 2, line.indexOf(":") - 3)
			if (arg_key in args) {
				parts.push({ "text": line.substr(0, plural_found), "arg_key": null });

				start_of_plural = plural_found + 2 + arg_key.length + 1
				whole_plural = line.substr(start_of_plural, line.indexOf(")") - start_of_plural);
				if (whole_plural.indexOf("/") === -1) {
					parts.push({ "text": "[[ plural error: " + arg_key + " ]]", "arg_key": null });
				}
				else if (args[arg_key] === 1) {
					parts.push({ "text": whole_plural.split("/", 2)[0], "arg_key": arg_key });
				}
				else {
					parts.push({ "text": whole_plural.split("/", 2)[1], "arg_key": arg_key });
				}

				line = line.substr(line.indexOf(")", plural_found) + 1);
			}
		}
		else {
			parts.push({ "text": line, "arg_key": null })
			line = "";
		}
	}
	return parts;
}

function  _get_suffixed_number(number) {
	if (("suffix_" + number) in lang) return number + lang["suffix_" + number];
	var key;
	for (var i = 100; i >= 10; i = i / 10) {
		key = "suffix_" + (number % i);
		if (key in lang) return number + lang[key];
	}
	return number;
}
