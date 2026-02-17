## DeepSeek Multi-Account Exporter & Regex Search Tool


DeepSeek Tool is a multi-threaded research utility designed for exporting chat history data and performing powerful regex-based searches either live during export or offline on previously exported datasets.

you can use this tool to automatically search through the content of DeepSeek account histories that you have access to (credentials). Enjoy!
# Demo
![demo.gif](https://github.com/alirezacsp/Historydumper/blob/65bc0ad153b2abea320add1c1c0ee5e324f9a129/demo.gif)

### Features

* Multi-account session processing
* Live regex search during data collection
* Offline search on exported datasets
* SQLite message database support
* Multi-threaded performance
* Proxy support
* Retry and timeout control

### Use Cases

* Security research
* Data auditing
* Forensics analysis
* Log mining
* Pattern discovery in large datasets

### Modes

**Export Mode**
Export chat sessions and messages from multiple accounts.

**Live Search Mode**
Run regex search while exporting data.

**Offline Search Mode**
Run regex search on previously exported files.

**Database Mode**
Store messages in SQLite for advanced querying.

### Requirements

```
pip install -r requirements.txt
```

### Basic Usage

Export data:

```
python deepseek_tool.py -a accounts.txt --output exports
```

Live search during export:

```
python deepseek_tool.py --accounts accounts.txt --patterns patterns.txt --live-search
```

Offline search:

```
python deepseek_tool.py --offline-search --patterns patterns.txt --output exports
```
## Tips

Try to write patterns specifically for a particular target.
Use the native language of the target destination in your searches.
The number of inputs should be large. 
there's no need to sort the list. Different credential formats are supported, such as:
https://chat.deepseek.com:user:pass
chat.deepseek.com:email:pass
email:pass:chat.deepseek.com
chat.deepseek.com | email:pass
etc…
This tool has been built based on DeepSeek's current architecture and may need to be updated in the future.

### Disclaimer

This tool is intended for authorized security research and data analysis only.
Do not use against systems or accounts without explicit permission.
