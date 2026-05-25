import asyncio
import argparse
import pathlib
import os
from datetime import timedelta


async def create_pano_albums(
    api_key: str,
    base_url: str,
    time_after: str | None,
    time_before: str | None,
    album: str | None,
    dry_run: bool,
):
    from immichpy import AsyncClient
    from immichpy.client.generated.models.metadata_search_dto import MetadataSearchDto
    from immichpy.client.generated.models.create_album_dto import CreateAlbumDto
    import dateparser

    assert time_after or time_before or album, (
        "At least one of time_after, time_before or album must be provided"
    )

    async with AsyncClient(
        api_key=api_key,
        base_url=base_url,
    ) as client:
        metadata = MetadataSearchDto()

        if time_after:
            metadata.taken_after = dateparser.parse(time_after)
            assert metadata.taken_after is not None, "Could not parse time_after"
        if time_before:
            metadata.taken_before = dateparser.parse(time_before)
            assert metadata.taken_before is not None, "Could not parse time_before"
        if album:
            assert False, "Filtering by album is not implemented yet"

        search_result = await client.search.search_assets(metadata_search_dto=metadata)

        assets = search_result.assets.items

        assert len(assets) > 0, "No assets found with the given filters"

        # sort assets by time
        assets.sort(key=lambda a: a.file_created_at)

        # remove ignored assets
        def ignore_filename(filename: str) -> bool:
            IGNORES = {"PHOTOSPHERE", "PORTRAIT", "NIGHT", "PANO"}
            for ignore in IGNORES:
                if ignore in filename:
                    return True
            return False

        assets = [a for a in assets if not ignore_filename(a.original_file_name)]

        panos = []
        index_first = 0
        file_created_at_prev = assets[0].file_created_at

        for index, a in enumerate(search_result.assets.items):
            if index == 0:
                continue
            # this photo does not belong to the same pano
            if a.file_created_at > file_created_at_prev + timedelta(seconds=6):
                if index - index_first > 1:
                    panos.append(assets[index_first:index])
                index_first = index
            file_created_at_prev = a.file_created_at
        # last photos
        if index - index_first > 1:
            panos.append(assets[index_first:index])

        for p in panos:
            pano_created_at: str = p[0].file_created_at.isoformat()
            pano_created_at, *_ = pano_created_at.split(".")  # remove milliseconds
            pano_created_at, *_ = pano_created_at.split("+")  # remove timezone
            album_name = f"pano-{pano_created_at}"
            print(
                "creating album",
                album_name,
                "with assets",
                [a.original_file_name for a in p],
            )
            if dry_run:
                continue
            await client.albums.create_album(
                create_album_dto=CreateAlbumDto(
                    albumName=album_name,
                    assetIds=[a.id for a in p],
                )
            )


async def delete_pano_albums(
    api_key: str,
    base_url: str,
    dry_run: bool,
    delete_assets: bool,
):
    from immichpy import AsyncClient
    from immichpy.client.generated.models.asset_bulk_delete_dto import (
        AssetBulkDeleteDto,
    )

    async with AsyncClient(
        api_key=api_key,
        base_url=base_url,
    ) as client:
        asset_ids = []

        albums = await client.albums.get_all_albums()
        for album in albums:
            if album.album_name.startswith("pano-"):
                if delete_assets:
                    # get album with assets
                    album = await client.albums.get_album_info(id=album.id)  # type: ignore
                    for asset in album.assets:
                        print("deleting asset", asset.original_file_name)
                        asset_ids.append(asset.id)

                print("deleting album", album.album_name)
                if not dry_run:
                    await client.albums.delete_album(id=album.id)  # type: ignore

        if delete_assets and not dry_run:
            delete_assets_dto = AssetBulkDeleteDto(ids=asset_ids)
            await client.assets.delete_assets(delete_assets_dto)  # type: ignore


async def run_command(*args):
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    returncode = await proc.wait()
    if returncode != 0:
        raise RuntimeError(f"Command {args} failed with return code {returncode}")


async def create_hugin_projects(
    api_key: str,
    base_url: str,
    path: pathlib.Path,
    num_threads: int,
    dry_run: bool,
):
    from immichpy import AsyncClient

    async with AsyncClient(
        api_key=api_key,
        base_url=base_url,
    ) as client:
        albums = await client.albums.get_all_albums()

        folder_queue: asyncio.Queue[pathlib.Path | None] = asyncio.Queue()

        async def worker():
            while True:
                folder = await folder_queue.get()

                # Sentinel value = shutdown
                if folder is None:
                    break

                print("creating Hugin project for folder", folder)
                pto = folder / "panorama.pto"

                await run_command(
                    "pto_gen",
                    "--output",
                    pto,
                    *folder.glob("*.*"),
                )

                await run_command(
                    "cpfind",
                    "--multirow",
                    "-o",
                    pto,
                    pto,
                )

                await run_command(
                    "celeste_standalone",
                    "-i",
                    pto,
                    "-o",
                    pto,
                )

                await run_command(
                    "cpclean",
                    "-o",
                    pto,
                    pto,
                )

                await run_command(
                    "autooptimiser",
                    "-a",
                    "-m",
                    "-l",
                    "-s",
                    "-q",
                    "-o",
                    pto,
                    pto,
                )

        workers = [asyncio.create_task(worker()) for i in range(num_threads)]

        for album in albums:
            if album.album_name.startswith("pano-"):
                print("downloading assets for album", album.album_name)

                if dry_run:
                    continue

                # get album with assets
                album = await client.albums.get_album_info(id=album.id)  # type: ignore

                album_path = path / f"{album.album_name}"

                for asset in album.assets:
                    await client.assets.download_asset_to_file(
                        id=asset.id,  # type: ignore
                        out_dir=album_path,
                    )

                await folder_queue.put(album_path)

        for _ in workers:
            await folder_queue.put(None)

        await asyncio.gather(*workers)


async def run_hugin(
    path: pathlib.Path,
    num_threads: int,
    dry_run: bool,
):
    folder_queue: asyncio.Queue[pathlib.Path | None] = asyncio.Queue()

    async def worker():
        while True:
            folder = await folder_queue.get()

            # Sentinel value = shutdown
            if folder is None:
                break

            print("running hugin stitcher in", folder)

            if dry_run:
                continue

            pto = folder / "panorama.pto"

            await run_command(
                "nice",
                "-n20",
                "hugin_executor",
                "--stitching",
                "--threads=4",
                pto,
            )

    workers = [asyncio.create_task(worker()) for i in range(num_threads)]

    for folder in path.glob("pano-*"):
        if folder.is_dir():
            await folder_queue.put(folder)

    for _ in workers:
        await folder_queue.put(None)

    await asyncio.gather(*workers)


async def add_metadata(
    path: pathlib.Path,
    num_threads: int,
    dry_run: bool,
):
    folder_queue: asyncio.Queue[pathlib.Path | None] = asyncio.Queue()

    async def worker():
        while True:
            folder = await folder_queue.get()

            # Sentinel value = shutdown
            if folder is None:
                break

            print("adding metadata to", folder)

            if dry_run:
                continue

            pto = folder / "panorama.pto"

            with open(pto) as f:
                for line in f:
                    if line.startswith("i "):
                        original_file = line.split()[-1]
                        original_file = original_file.removeprefix('n"')
                        original_file = original_file.removesuffix('"')
                        original_file = folder / original_file
                        break
                else:
                    print("could not find original file in pto", pto)
                    continue

            tif = next(folder.glob("*.tif"))
            jpg = folder / f"{folder.name}.jpg"

            await run_command(
                "magick",
                "-quality",
                "95%",
                tif,
                jpg,
            )
            await run_command(
                "exiftool",
                "-TagsFromFile",
                original_file,
                "-all:all",
                "-overwrite_original",
                "--",
                jpg,
            )

    workers = [asyncio.create_task(worker()) for i in range(num_threads)]

    for folder in path.glob("pano-*"):
        if folder.is_dir():
            await folder_queue.put(folder)

    for _ in workers:
        await folder_queue.put(None)

    await asyncio.gather(*workers)


# async def upload_panoramas(
#     api_key: str,
#     base_url: str,
#     path: pathlib.Path,
#     dry_run: bool,
# ):
#     from immichpy import AsyncClient

#     async with AsyncClient(
#         api_key=api_key,
#         base_url=base_url,
#     ) as client:
#         for folder in path.glob("pano-*"):
#             if not folder.is_dir():
#                 continue
#             jpg = folder / f"{folder.name}.jpg"

#             client.assets.upload_asset()


if __name__ == "__main__":

    def environ_or_required(key):
        return (
            {"default": os.environ.get(key)}
            if os.environ.get(key)
            else {"required": True}
        )

    parser = argparse.ArgumentParser()

    ###############################################################################################

    parser.add_argument("--api-key", **environ_or_required("IMMICH_API_KEY"))  # type: ignore
    parser.add_argument("--base-url", **environ_or_required("IMMICH_BASE_URL"))  # type: ignore
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="don't actually execute, just print what would be done",
    )
    parser.add_argument(
        "--threads", type=int, default=4, help="number of threads to use for processing"
    )

    commands = parser.add_subparsers(dest="command", required=True)

    ###############################################################################################

    cmd_create = commands.add_parser(
        "create-pano-albums",
        help="group photos into albums if they belong to the same pano",
    )
    cmd_create.add_argument(
        "--time-after", help="only consider photos created after this time"
    )
    cmd_create.add_argument(
        "--time-before", help="only consider photos created before this time"
    )
    cmd_create.add_argument("--album", help="only consider photos in this album")

    ###############################################################################################

    cmd_delete = commands.add_parser(
        "delete-pano-albums",
        help="delete pano albums",
    )
    cmd_delete.add_argument(
        "--delete-assets",
        action="store_true",
        help="delete assets in pano albums instead of just albums",
    )

    ###############################################################################################

    cmd_create_hugin_projects = commands.add_parser(
        "create-hugin-projects",
        help="create Hugin projects from pano albums",
    )
    cmd_create_hugin_projects.add_argument(
        "--path", help="path to save Hugin projects", default=os.getcwd()
    )

    ###############################################################################################

    cmd_run_hugin = commands.add_parser(
        "run-hugin",
        help="run Hugin stitcher on created projects",
    )
    cmd_run_hugin.add_argument(
        "--path", help="path where panorama folders are located", default=os.getcwd()
    )

    ###############################################################################################

    cmd_add_metadata = commands.add_parser(
        "add-metadata",
        help="add metadata from original files to stitched panoramas",
    )
    cmd_add_metadata.add_argument(
        "--path", help="path where panorama folders are located", default=os.getcwd()
    )

    ###############################################################################################

    args = parser.parse_args()

    if args.command == "create-pano-albums":
        asyncio.run(
            create_pano_albums(
                api_key=args.api_key,
                base_url=args.base_url,
                time_after=args.time_after,
                time_before=args.time_before,
                album=args.album,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "delete-pano-albums":
        asyncio.run(
            delete_pano_albums(
                api_key=args.api_key,
                base_url=args.base_url,
                dry_run=args.dry_run,
                delete_assets=args.delete_assets,
            )
        )
    elif args.command == "create-hugin-projects":
        asyncio.run(
            create_hugin_projects(
                api_key=args.api_key,
                base_url=args.base_url,
                path=pathlib.Path(args.path),
                num_threads=args.threads,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "run-hugin":
        asyncio.run(
            run_hugin(
                path=pathlib.Path(args.path),
                num_threads=args.threads,
                dry_run=args.dry_run,
            )
        )
    elif args.command == "add-metadata":
        asyncio.run(
            add_metadata(
                path=pathlib.Path(args.path),
                num_threads=args.threads,
                dry_run=args.dry_run,
            )
        )
    else:
        raise ValueError("unknown command", args.command)
