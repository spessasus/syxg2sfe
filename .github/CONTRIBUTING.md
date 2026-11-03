## Contributing to syxg2sfe

First, I want to thank you for wanting to help develop syxg2sfe.
I really appreciate your contribution :-)

Here are some tips about contributing:

### Reporting a bug

Use the GitHub issue forms and please avoid using the "Blank" template unless absolutely necessary.
Never attach the plugin DLL or a generated bank: they contain Yamaha's software and samples.

### Contributing a patch/enhancement

Build a bank with your change (`python3 build.py --dll input/syxg50.dll --output out`) and compare the voices you
touched with the plugin. Run `npm run build` (type check) before opening the pull request.

### License notice

When submitting a pull request to `syxg2sfe`, you are giving [Spessasus](https://github.com/spessasus) the right to license
your contributions under the [Apache License 2.0](../LICENSE), and you irrevocably permit a potential
future re-licensing of `syxg2sfe`, containing your contributions, under any open source license.
