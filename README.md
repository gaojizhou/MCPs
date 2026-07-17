# MCPs

A growing collection of independent [Model Context Protocol (MCP)](https://modelcontextprotocol.io/)
servers. Each server owns its implementation, dependencies, installation guide, configuration, and
security notes, so users can install one MCP without pulling in unrelated runtimes.

## Available MCPs

| MCP | What it provides | Documentation |
| --- | --- | --- |
| So I Can See You | Gives AI a pair of eyes that can look around, seek someone, and return a carefully framed photograph | [Install and use](mcps/so-i-can-see-you/README.md) |

## Repository layout

```text
MCPs/
├── README.md
├── LICENSE
├── .gitignore
└── mcps/
    ├── README.md
    └── so-i-can-see-you/
        ├── README.md
        ├── so_i_can_see_you_mcp.py
        └── requirements.txt
```

The repository root contains only collection-level documentation and policy. Every runnable server
lives in `mcps/<mcp-name>/` and remains self-contained.

## Using an MCP

Open the server's README and follow its requirements, installation steps, and client configuration.
Do not assume that every MCP in this repository uses the same language, package manager, virtual
environment, or transport.

So I Can See You is documented at
[`mcps/so-i-can-see-you/README.md`](mcps/so-i-can-see-you/README.md).

## Adding a new MCP

Create a self-contained directory under `mcps/` using a descriptive lowercase, hyphen-separated
name. Follow the [MCP contribution conventions](mcps/README.md), then add the new server to the table
above.

## Security and issues

Never commit passwords, tokens, private keys, private camera footage, or configuration files that
contain credentials. Each MCP must document the resources it accesses, the external actions it can
perform, and the safe way to provide secrets.

If you find a problem, please open a redacted Issue containing the MCP name, runtime environment,
reproduction steps, expected behavior, and actual behavior. Do not attach secrets or unredacted
private data.

## License

This repository is licensed under the [GNU Affero General Public License v3.0 only](LICENSE)
(`AGPL-3.0-only`). Modified covered versions must remain under the same license, and operators who
let users interact with a modified version over a network must offer those users the corresponding
source code as required by section 13.
